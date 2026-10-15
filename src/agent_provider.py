import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol


class AgentProvider(Protocol):
    def complete(self, role: str, instructions: str, payload: dict[str, Any]) -> str:
        ...


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        parsed_url = urllib.parse.urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed_url.scheme != "https" and parsed_url.hostname not in local_hosts:
            raise ValueError("AGENT_BASE_URL must use HTTPS except for localhost endpoints.")
        if not parsed_url.netloc or not model.strip() or not api_key.strip():
            raise ValueError("Provider URL, API key, and model are required.")
        endpoint_path = parsed_url.path.rstrip("/") + "/chat/completions"
        self.endpoint = urllib.parse.urlunsplit((
            parsed_url.scheme,
            parsed_url.netloc,
            endpoint_path,
            parsed_url.query,
            "",
        ))
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def manifest_metadata(self):
        parsed_endpoint = urllib.parse.urlsplit(self.endpoint)
        hostname = parsed_endpoint.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed_endpoint.port
        except ValueError:
            port = None
        netloc = hostname if port is None else f"{hostname}:{port}"
        return {
            "name": "openai-compatible",
            "model": self.model,
            "endpoint": urllib.parse.urlunsplit((
                parsed_endpoint.scheme,
                netloc,
                parsed_endpoint.path,
                "",
                "",
            )),
        }

    def complete(self, role: str, instructions: str, payload: dict[str, Any]) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps({"agent_role": role, **payload})},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Agent provider returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("Agent provider request failed.") from error
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("Agent provider returned an invalid chat-completion response.") from error
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Agent provider returned empty content.")
        return content


def configured_agent_provider(environ=None):
    environment = os.environ if environ is None else environ
    provider_name = environment.get("AGENT_PROVIDER", "").strip().casefold()
    if not provider_name:
        return None
    if provider_name != "openai-compatible":
        raise ValueError("AGENT_PROVIDER must be 'openai-compatible' or unset.")
    required = {
        "AGENT_BASE_URL": environment.get("AGENT_BASE_URL", "").strip(),
        "AGENT_API_KEY": environment.get("AGENT_API_KEY", "").strip(),
        "AGENT_MODEL": environment.get("AGENT_MODEL", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Missing provider configuration: " + ", ".join(missing))
    return OpenAICompatibleProvider(
        required["AGENT_BASE_URL"],
        required["AGENT_API_KEY"],
        required["AGENT_MODEL"],
    )