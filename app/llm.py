"""Claude calls: prompt files, structured output, and a log row per call.

Prompts live in prompts/<name>.md: YAML front matter (model, effort, max_tokens)
followed by the system prompt. Inputs are passed in the user turn as tagged blocks.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import yaml
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import LlmCall

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    def __init__(self, message: str, call_id: int | None = None):
        super().__init__(message)
        self.call_id = call_id


@dataclass(frozen=True)
class Prompt:
    name: str
    model: str
    effort: str | None
    max_tokens: int
    system: str
    sha: str


def load_prompt(prompts_dir: Path, name: str) -> Prompt:
    raw = (prompts_dir / f"{name}.md").read_text()
    if not raw.startswith("---\n"):
        raise ValueError(f"{name}.md: missing YAML front matter")
    _, front, body = raw.split("---\n", 2)
    meta = yaml.safe_load(front) or {}
    return Prompt(
        name=name,
        model=meta["model"],
        effort=meta.get("effort"),
        max_tokens=int(meta.get("max_tokens", 16000)),
        system=body.strip(),
        sha=hashlib.sha256(raw.encode()).hexdigest()[:12],
    )


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema → the strict subset structured outputs accept.

    Inlines $defs, closes every object, and drops titles/defaults.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object":
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}))
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


# Content-block helpers --------------------------------------------------------


def text_block(tag: str, text: str) -> dict:
    return {"type": "text", "text": f"<{tag}>\n{text}\n</{tag}>"}


def pdf_block(data: bytes, title: str) -> dict:
    return {
        "type": "document",
        "title": title,
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode()},
    }


def plain_document_block(text: str, title: str) -> dict:
    return {"type": "document", "title": title, "source": {"type": "text", "media_type": "text/plain", "data": text}}


def image_block(data: bytes, media_type: str = "image/jpeg") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()}}


# Transport --------------------------------------------------------------------


@dataclass
class RawResult:
    text: str
    stop_reason: str | None
    served_model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    extra: dict = field(default_factory=dict)


class Transport(Protocol):
    def send(self, *, prompt: Prompt, content: list[dict], schema: dict) -> RawResult: ...


class AnthropicTransport:
    """Real API. Streams so long extractions don't hit request timeouts."""

    def __init__(self):
        import anthropic

        self.client = anthropic.Anthropic()

    def send(self, *, prompt: Prompt, content: list[dict], schema: dict) -> RawResult:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if prompt.effort:
            output_config["effort"] = prompt.effort
        with self.client.beta.messages.stream(
            model=prompt.model,
            max_tokens=prompt.max_tokens,
            system=prompt.system,
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            # A declined request is re-run on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        text = next((b.text for b in msg.content if b.type == "text"), "")
        return RawResult(
            text=text,
            stop_reason=msg.stop_reason,
            served_model=msg.model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
        )


# The one entry point ----------------------------------------------------------


class Llm:
    def __init__(self, prompts_dir: Path, transport: Transport | None = None):
        self.prompts_dir = prompts_dir
        self._transport = transport

    @property
    def transport(self) -> Transport:
        if self._transport is None:  # created lazily so the web app starts without a key
            self._transport = AnthropicTransport()
        return self._transport

    def call(self, db: Session, *, prompt_name: str, purpose: str, content: list[dict], output: type[T]) -> tuple[T, LlmCall]:
        """Run a prompt, log it, and return the validated output plus its log row.

        Raises LlmError (with the logged call id) on API errors, refusals, truncation
        or invalid output. The log row is committed either way.
        """
        prompt = load_prompt(self.prompts_dir, prompt_name)
        log = LlmCall(
            purpose=purpose,
            prompt_name=prompt.name,
            prompt_sha=prompt.sha,
            model=prompt.model,
            effort=prompt.effort,
            request_text="\n\n".join(b["text"] for b in content if b.get("type") == "text"),
        )
        db.add(log)
        try:
            raw = self.transport.send(prompt=prompt, content=content, schema=strict_schema(output))
            log.served_model, log.stop_reason = raw.served_model, raw.stop_reason
            log.input_tokens, log.output_tokens = raw.input_tokens, raw.output_tokens
            log.response_text = raw.text
            if raw.stop_reason == "refusal":
                raise LlmError("the model declined this request")
            if raw.stop_reason == "max_tokens":
                raise LlmError("the response was cut off at max_tokens; raise it in the prompt file")
            parsed = output.model_validate(json.loads(raw.text))
        except LlmError as e:
            log.error = str(e)
            db.commit()
            raise LlmError(str(e), log.id) from None
        except Exception as e:  # API, network and validation errors all end up here
            log.error = f"{type(e).__name__}: {e}"
            if "authentication" in str(e).lower():
                log.error = "No Anthropic credentials: set ANTHROPIC_API_KEY and restart. (" + log.error + ")"
            db.commit()
            raise LlmError(log.error, log.id) from e
        db.commit()
        return parsed, log
