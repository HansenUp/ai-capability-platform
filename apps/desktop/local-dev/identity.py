"""Owned, ephemeral Keycloak using the project's cached image and realm scopes."""

import json
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
ISSUER = "http://127.0.0.1:8180/realms/ai-platform"
CALLBACK = "http://127.0.0.1:18765/callback"
CLIENT = "f8-desktop"
IMAGE = "sha256:3f88bb5495536996bf35c187ee63da61c448ff53fbf1e26d7f2ae604cd6578e2"
# Public disposable fixture credentials, never a real user's account.
PASSWORD = "f8-local-development-only"  # noqa: S105 -- public disposable fixture, never a real credential
USERS = [
    ("f8-alice", "ff800000-0000-4000-8000-000000000001", "default"),
    ("f8-bob", "ff800000-0000-4000-8000-000000000002", "t2"),
]


def docker(*args):
    return subprocess.run(  # noqa: S603 -- fixed local Docker CLI; arguments are owned lifecycle operations
        [
            "/Applications/Docker.app/Contents/Resources/bin/docker",
            "--host",
            "unix://" + str(Path.home() / ".docker/run/docker.sock"),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


class LocalIdentity:
    def __init__(self, *, verify=False):
        self.owner = uuid.uuid4().hex
        self.name = "aip-f8-idp-" + self.owner[:12]
        self.network = self.name + "-net"
        self.root = None
        self.container = None
        self.network_created = False
        self.verify = verify

    def __enter__(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 8180))  # Never replace an existing local IdP.
        self.root = Path(tempfile.mkdtemp(prefix="aip-f8-idp-", dir="/private/tmp"))
        self.root.chmod(0o700)
        try:
            realm = json.loads((ROOT / "services/identity/realm-export.json").read_text())
            realm["clients"].append(json.loads((Path(__file__).parent / "keycloak-client.json").read_text()))
            realm["accessTokenLifespan"] = 12 if self.verify else 120
            realm["users"] = [
                {
                    "id": sub,
                    "username": name,
                    "enabled": True,
                    "emailVerified": True,
                    "firstName": "F8",
                    "lastName": name,
                    "email": name + "@example.invalid",
                    "attributes": {"tenant": [tenant]},
                    "requiredActions": [],
                    "credentials": [{"type": "password", "value": PASSWORD, "temporary": False}],
                }
                for name, sub, tenant in USERS
            ]
            (self.root / "realm.json").write_text(json.dumps(realm))
            # Docker Desktop's bind-mount server must be able to read this public fixture.
            (self.root / "realm.json").chmod(0o644)
            native = subprocess.run(  # noqa: S603 -- fixed /bin/cat on the owned public realm fixture
                ["/bin/cat", str(self.root / "realm.json")], check=True, capture_output=True
            ).stdout
            if native != (self.root / "realm.json").read_bytes():
                raise RuntimeError("Realm byte views differ; no container started")
            docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
            # Docker Desktop did not forward published ports on the internal-only
            # bridge. Keep one owned bridge and publish only the loopback listener.
            docker("network", "create", "--label", "aip.f8.owner=" + self.owner, self.network)
            self.network_created = True
            self.container = docker(
                "run",
                "-d",
                "--rm",
                "--pull",
                "never",
                "--name",
                self.name,
                "--label",
                "aip.f8.owner=" + self.owner,
                "--network",
                self.network,
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=5m",
                "-p",
                "127.0.0.1:8180:8080",
                "--mount",
                f"type=bind,source={self.root / 'realm.json'},target=/opt/keycloak/data/import/realm.json,readonly",
                IMAGE,
                "start-dev",
                "--import-realm",
                "--hostname=http://127.0.0.1:8180",
            )
            deadline = time.monotonic() + 90
            with httpx.Client(trust_env=False, timeout=2) as client:
                while True:
                    try:
                        response = client.get(ISSUER + "/.well-known/openid-configuration")
                        if response.status_code == 200 and response.json()["issuer"] == ISSUER:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Owned Keycloak failed readiness")
                    time.sleep(0.25)
            self.record("READY")
            return self
        except BaseException:
            if self.container:
                evidence = ROOT / "apps/desktop/native-candidate/g2-b-preflight/keycloak-startup.log"
                try:
                    evidence.write_text(docker("logs", "--tail", "60", self.container))
                except subprocess.CalledProcessError:
                    evidence.write_text("Owned container exited before log collection")
            self.__exit__(None, None, None)
            raise

    def record(self, status):
        target = ROOT / "apps/desktop/native-candidate/g2-b-preflight/local-idp-owner.json"
        target.write_text(
            json.dumps(
                {
                    "status": status,
                    "owner": self.owner,
                    "container": self.container,
                    "network": self.network,
                    "directory": str(self.root) if self.root else None,
                    "image": IMAGE,
                },
                indent=2,
            )
        )
        target.chmod(0o600)

    def __exit__(self, *_):
        if self.container:
            owner = docker("inspect", self.container, "--format", '{{index .Config.Labels "aip.f8.owner"}}')
            if owner != self.owner:
                raise RuntimeError("Container ownership changed; cleanup refused")
            docker("stop", "--time", "15", self.container)
            self.container = None
        if self.network_created:
            owner = docker("network", "inspect", self.network, "--format", '{{index .Labels "aip.f8.owner"}}')
            if owner != self.owner:
                raise RuntimeError("Network ownership changed; cleanup refused")
            docker("network", "rm", self.network)
            self.network_created = False
        if self.root:
            shutil.rmtree(self.root)
            self.root = None
        self.record("CLEANED")
