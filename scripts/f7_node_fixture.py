"""Disposable browser-only F7 authority and node journals, no external transport."""

import asyncio
import json
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def configure(app):
    from console_api import owner_auth
    from console_api.isolated_node_control import Store
    from runtime.node.isolated_control import LifecycleNode, SyntheticNode
    from runtime.node.lifecycle import NodeLifecycle

    store = Store()
    actors = [json.dumps([owner_auth._profile.issuer, "default", subject], separators=(",", ":")) for subject in ("alice", "bob")]
    nodes = {
        name: SyntheticNode(name)
        for name in ("f7-node-normal", "f7-node-loss", "f7-node-reload", "f7-node-tabs", "f7-node-timeout", "f7-node-limit")
    }
    for name in list(nodes):
        if name == "f7-node-timeout":
            continue
        nodes[name].close()
        worker = SimpleNamespace(paused=False, idle=asyncio.Event())
        worker.idle.set()
        nodes[name] = LifecycleNode(name, NodeLifecycle(SimpleNamespace(worker=worker), None, durable=False))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for name in nodes:
        store.seed(name, actors)
        store.node_keys[name] = ("https://f7-node.test", "f7-test", key.public_key())

    def signed(name, evidence):
        stamp = int(time.time())
        return jwt.encode(
            dict(
                iss="https://f7-node.test",
                aud="f7-isolated-node-evidence",
                sub=name,
                identity_type="node",
                iat=stamp,
                nbf=stamp,
                exp=stamp + 60,
                evidence=evidence,
            ),
            key,
            algorithm="RS256",
            headers={"kid": "f7-test"},
        )

    async def apply(op, authority):
        if op["node"] == "f7-node-timeout" and op["action"] == "drain":
            nodes[op["node"]].active = 1
        node = nodes[op["node"]]
        value = await node.apply_lifecycle(op, authority, 30) if isinstance(node, LifecycleNode) else node.apply(op, authority)
        if op["node"] == "f7-node-timeout":
            # Synthetic in-flight task finishes after the timeout evidence was persisted.
            nodes[op["node"]].active = 0
        if op["node"] == "f7-node-loss":
            raise OSError("synthetic application response lost")
        return signed(op["node"], value)

    def read(name, attempt):
        evidence = nodes[name].read(attempt)
        return signed(name, evidence) if evidence else None

    def observation(name, attempt):
        try:
            return signed(name, nodes[name].observe_drained(attempt))
        except ValueError:
            return None

    store.read_observation = observation
    store.apply_node = apply
    store.read_evidence = read
    app.state.isolated_node_control = store

    def close():
        store.close()
        for node in nodes.values():
            node.close()

    app.state.close_f7_fixture = close
