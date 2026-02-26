"""Configuration for NATS connection."""

import os
from dataclasses import dataclass, field


@dataclass
class NatsConfig:
    """Configuration loaded from environment variables."""

    servers: list[str] = field(default_factory=lambda: ["nats://localhost:4222"])
    user: str = ""
    password: str = ""

    @staticmethod
    def _normalize_server(server: str) -> str:
        """Ensure server has nats:// scheme and port."""
        server = server.strip()
        if not server.startswith(("nats://", "tls://", "ws://", "wss://")):
            server = f"nats://{server}"
        if server.count(":") == 1:  # has scheme but no port
            server = f"{server}:4222"
        return server

    @classmethod
    def from_env(cls) -> "NatsConfig":
        """Load configuration from environment variables."""
        servers_str = os.getenv("NATS_SERVER_IP", "nats://localhost:4222")
        servers = [cls._normalize_server(s) for s in servers_str.split(",")]

        return cls(
            servers=servers,
            user=os.getenv("NATS_USER", ""),
            password=os.getenv("NATS_PASS", ""),
        )
