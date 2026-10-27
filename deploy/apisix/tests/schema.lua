ngx.config.prefix = function() return "/usr/local/apisix/" end
local plugin = require("apisix.plugins.ai-node-routing")
local json = require("cjson.safe")
local file = assert(io.open("/usr/local/apisix/conf/apisix.yaml", "r"))
local content = file:read("*a")
file:close()
local data = assert(json.decode((content:gsub("\n#END\n", ""))))
local original = data.routes[1].plugins["ai-node-routing"]
assert(plugin.check_schema(original))

local function rejected(change)
    local conf = assert(json.decode(json.encode(original)))
    change(conf)
    local ok = plugin.check_schema(conf)
    assert(not ok, "unsafe configuration must be rejected")
end

rejected(function(conf) conf.nodes.a.role = "worker" end)
rejected(function(conf) conf.nodes.a.state_pool = "different-database" end)
rejected(function(conf) conf.nodes.a.host = "untrusted.example" end)
rejected(function(conf) conf.nodes.a.port = 65536 end)
rejected(function(conf) conf.fallback_nodes = {"unknown-node"} end)
rejected(function(conf) conf.nodes = {} end)
rejected(function(conf) conf.routing_token = "" end)
print("PASS real Lua schema: worker, mixed state pool, hostname, bad port and unknown fallback rejected")
