local core = require("apisix.core")
local upstream = require("apisix.upstream")
local http = require("resty.http")
local openidc = require("resty.openidc")
local jwt = require("resty.jwt")
local cache = require("resty.lrucache").new(128)

local identifier = {type = "string", minLength = 1, maxLength = 128, pattern = "^[A-Za-z0-9_.:-]+$"}
local node_schema = {
    type = "object",
    properties = {
        host = {type = "string", format = "ipv4"},
        port = {type = "integer", minimum = 1, maximum = 65535},
        role = {type = "string", enum = {"api", "all"}},
        state_pool = identifier,
    },
    required = {"host", "port", "role", "state_pool"},
    additionalProperties = false,
}
local schema = {
    type = "object",
    properties = {
        tenant_id = identifier,
        state_pool = identifier,
        routing_url = {type = "string", pattern = "^https?://"},
        routing_token = {type = "string", minLength = 32},
        issuer = {type = "string", minLength = 1},
        audience = {type = "string", minLength = 1},
        jwks_uri = {type = "string", pattern = "^https?://"},
        tenant_claim = {type = "string", default = "tenant", minLength = 1},
        refresh_seconds = {type = "integer", minimum = 1, maximum = 300, default = 10},
        stale_warning_seconds = {type = "integer", minimum = 1, default = 60},
        nodes = {type = "object", minProperties = 1, maxProperties = 64, additionalProperties = node_schema},
        fallback_nodes = {type = "array", uniqueItems = true, items = identifier},
        scheme = {type = "string", enum = {"http"}, default = "http"},
    },
    required = {"tenant_id", "state_pool", "routing_url", "routing_token", "issuer", "audience", "jwks_uri", "nodes", "fallback_nodes"},
    additionalProperties = false,
    encrypt_fields = {"routing_token"},
}
local snapshot_schema = {
    type = "object",
    properties = {
        tenant_id = identifier,
        default_group = identifier,
        generated_at = {type = "number", minimum = 0},
        groups = {type = "object", additionalProperties = {type = "array", uniqueItems = true, items = identifier}},
        bindings = {type = "object", additionalProperties = {
            type = "object", additionalProperties = {type = "integer", minimum = 0, maximum = 100},
        }},
    },
    required = {"tenant_id", "default_group", "generated_at", "groups", "bindings"},
    additionalProperties = false,
}
local _M = {version = 0.1, priority = 2600, name = "ai-node-routing", schema = schema}

function _M.check_schema(conf)
    local ok, err = core.schema.check(schema, conf)
    if not ok then
        return false, err
    end
    for _, node in pairs(conf.nodes) do
        if node.state_pool ~= conf.state_pool then
            return false, "all HTTP nodes must share the configured state_pool"
        end
    end
    for _, id in ipairs(conf.fallback_nodes) do
        if not conf.nodes[id] then
            return false, "fallback node is not allowlisted"
        end
    end
    return true
end

local function refresh(conf, state)
    local client = http.new()
    client:set_timeout(2000)
    local response = client:request_uri(conf.routing_url, {
        method = "GET", headers = {Authorization = "Bearer " .. conf.routing_token},
        ssl_verify = true, keepalive = false,
    })
    if not response or response.status ~= 200 or #response.body > 1048576 then
        core.log.warn("ai-node-routing: control-plane fetch failed; preserving tenant cache")
        return
    end
    local data = core.json.decode(response.body)
    if not data or not core.schema.check(snapshot_schema, data) or data.tenant_id ~= conf.tenant_id then
        core.log.warn("ai-node-routing: rejected invalid or cross-tenant snapshot")
        return
    end
    if state.snapshot and data.generated_at < state.snapshot.generated_at then
        core.log.warn("ai-node-routing: rejected older snapshot")
        return
    end
    state.snapshot, state.updated = data, ngx.now()
end

local function snapshot(conf, key)
    local state = cache:get(key)
    if not state then
        state = {last_attempt = ngx.now()}
        cache:set(key, state)
        refresh(conf, state)
    elseif ngx.now() - state.last_attempt >= conf.refresh_seconds then
        state.last_attempt = ngx.now()
        if state.updated and ngx.now() - state.updated > conf.stale_warning_seconds then
            core.log.warn("ai-node-routing: stale tenant routing cache")
        end
        local ok = ngx.timer.at(0, function(premature)
            if not premature then
                refresh(conf, state)
            end
        end)
        if not ok then
            core.log.warn("ai-node-routing: refresh timer unavailable")
        end
    end
    return state.snapshot, state
end

local function printable(value)
    return type(value) == "string" and #value > 0 and #value <= 256 and not value:find("[%c]")
end

local function identity(conf, ctx, key)
    local authorization = core.request.header(ctx, "Authorization")
    if type(authorization) ~= "string" then
        return nil
    end
    local token = authorization:match("^[Bb][Ee][Aa][Rr][Ee][Rr] +([^ ]+)$")
    if not token then
        return nil
    end
    local parsed = jwt:load_jwt(token)
    if not parsed.valid or parsed.header.alg ~= "RS256" or not printable(parsed.header.kid) then
        return nil
    end
    local verified, claims, err = pcall(openidc.bearer_jwt_verify, {
        discovery = {issuer = conf.issuer, jwks_uri = conf.jwks_uri},
        token_signing_alg_values_expected = {"RS256"}, accept_none_alg = false,
        accept_unsupported_alg = false, ssl_verify = true, timeout = 2000,
        jwk_expires_in = 60,
        cache_segment = key, jwt_verification_cache_ignore = true,
    })
    local now = ngx.time()
    if not verified or err or not claims or claims.iss ~= conf.issuer or not printable(claims.sub)
        or claims[conf.tenant_claim] ~= conf.tenant_id
        or type(claims.exp) ~= "number" or claims.exp <= now
        or type(claims.iat) ~= "number" or claims.iat > now
        or (claims.nbf ~= nil and (type(claims.nbf) ~= "number" or claims.nbf > now)) then
        return nil
    end
    local audience = claims.aud == conf.audience
    if type(claims.aud) == "table" then
        for _, value in ipairs(claims.aud) do
            audience = audience or value == conf.audience
        end
    end
    if not audience then
        return nil
    end
    local client = claims.client_id or claims.azp
    local subject, actor = "human:" .. claims.sub, nil
    if claims.client_id then
        if not printable(client) then
            return nil
        end
        subject, actor = "app:" .. client, "app:" .. client
    else
        local act = claims.act
        if type(act) == "table" then
            act = act.sub
        end
        if act ~= nil and not printable(act) then
            return nil
        end
        actor = act
        if not actor and client then
            if not printable(client) then
                return nil
            end
            actor = "app:" .. client
        end
    end
    return {subject = subject, actor = actor}
end

local function allowed(ids, conf)
    local result, seen = {}, {}
    for _, id in ipairs(ids or {}) do
        if conf.nodes[id] and not seen[id] then
            seen[id] = true
            result[#result + 1] = id
        end
    end
    table.sort(result)
    return result
end

local function choose(conf, data, agent)
    if not data then
        return conf.fallback_nodes
    end
    if not agent then
        local ids = {}
        for _, group in pairs(data.groups) do
            for _, id in ipairs(group) do
                ids[#ids + 1] = id
            end
        end
        return allowed(ids, conf)
    end
    local binding = data.bindings[agent]
    if binding == nil then
        return allowed(data.groups[data.default_group], conf)
    end
    local groups, total = {}, 0
    for name, weight in pairs(binding) do
        local nodes = allowed(data.groups[name], conf)
        if weight > 0 and #nodes > 0 then
            groups[#groups + 1] = {name = name, weight = weight, nodes = nodes}
            total = total + weight
        end
    end
    if total == 0 then
        return {}
    end
    table.sort(groups, function(left, right) return left.name < right.name end)
    local selected = math.random(total)
    for _, group in ipairs(groups) do
        selected = selected - group.weight
        if selected <= 0 then
            return group.nodes
        end
    end
    return {}
end

local function healthy_nodes(conf, state)
    state.health = state.health or {}
    local nodes = {}
    for id, node in pairs(conf.nodes) do
        local address = node.host .. ":" .. node.port
        local previous = state.health[address]
        if not previous or ngx.now() - previous.checked >= 1 then
            local client = http.new()
            client:set_timeout(500)
            local response = client:request_uri(conf.scheme .. "://" .. address .. "/healthz", {
                method = "GET", ssl_verify = true, keepalive = false,
            })
            previous = {checked = ngx.now(), healthy = response and response.status == 200}
            state.health[address] = previous
        end
        if previous.healthy then
            nodes[id] = node
        end
    end
    return nodes, state.health
end

function _M.access(conf, ctx)
    local headers = ngx.req.get_headers(0)
    for name in pairs(headers) do
        local lower = name:lower():gsub("_", "-")
        if lower:match("^x%-ai%-") or lower:match("^x%-console%-")
            or lower == "x-userinfo" or lower == "x-id-token" or lower == "x-access-token" then
            core.request.set_header(ctx, name, nil)
        end
    end
    local key = ngx.md5(core.json.encode(conf))
    local principal = identity(conf, ctx, key)
    if not principal then
        ngx.header["WWW-Authenticate"] = "Bearer"
        return 401, {message = "invalid caller identity"}
    end
    local path, method, agent = ctx.var.uri, ngx.req.get_method(), nil
    if (path == "/v1/messages" or path == "/v1/runs") and method == "POST" then
        local body, err = core.request.get_body(1048576, ctx)
        if err then
            return 413, {message = "request body too large"}
        end
        local data = body and core.json.decode(body)
        if type(data) ~= "table" or not printable(data.agent_id) then
            return 400, {message = "agent_id required"}
        end
        agent = data.agent_id
    elseif not ((method == "GET" and (path == "/v1/agents" or path == "/v1/runs"))
        or (method == "GET" and path:match("^/v1/runs/[%w_-]+$"))
        or (method == "GET" and (path:match("^/v1/runs/[%w_-]+/events$") or path:match("^/v1/runs/[%w_-]+/stream$")))
        or (method == "POST" and path:match("^/v1/runs/[%w_-]+/[a-z]+$")
            and ({approve = true, reject = true, resume = true, cancel = true, input = true, feedback = true})[path:match("/([^/]+)$")])) then
        return 403, {message = "route outside Runtime public API"}
    end
    local data, state = snapshot(conf, key)
    local healthy, health = healthy_nodes(conf, state)
    local eligible = core.table.clone(conf)
    eligible.nodes = healthy
    eligible.fallback_nodes = allowed(conf.fallback_nodes, eligible)
    local ids = choose(eligible, data, agent)
    if #ids == 0 then
        return 503, {message = "no eligible node in tenant state pool"}
    end
    local nodes = {}
    for _, id in ipairs(ids) do
        local node = conf.nodes[id]
        nodes[#nodes + 1] = {host = node.host, port = node.port, weight = 1}
    end
    local up = {
        type = "roundrobin", scheme = conf.scheme, nodes = nodes, retries = 0,
        timeout = {connect = 2, send = 30, read = 300},
    }
    local ok = upstream.check_schema(up)
    if not ok then
        return 500, {message = "invalid allowlisted upstream"}
    end
    core.request.set_header(ctx, "X-AI-Subject", principal.subject)
    core.request.set_header(ctx, "X-AI-Tenant", conf.tenant_id)
    core.request.set_header(ctx, "X-AI-Actor", principal.actor)
    ctx.ai_route_health = health
    upstream.set(ctx, "ai-node-routing#" .. key .. "#" .. table.concat(ids, ","), ngx.md5(core.json.encode(up)), up)
end

function _M.log(conf, ctx)
    local status = tonumber(ctx.var.upstream_status)
    if ctx.ai_route_health and ctx.balancer_ip and (status == 429 or (status and status >= 500)) then
        ctx.ai_route_health[ctx.balancer_ip .. ":" .. ctx.balancer_port] = {checked = ngx.now(), healthy = false}
    end
end

return _M
