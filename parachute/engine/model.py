from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class Destroyed:
    """Everything an action takes away, in the graph's own coordinates."""

    paths: set = field(default_factory=set)
    refs: set = field(default_factory=set)
    ref_prefixes: set = field(default_factory=set)
    repoints: dict = field(default_factory=dict)
    repos: set = field(default_factory=set)
    prunable: set = field(default_factory=set)
    stores: set = field(default_factory=set)

    def merge(self, other: "Destroyed") -> None:
        for f in fields(self):
            getattr(self, f.name).update(getattr(other, f.name))

    def is_empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = dict(sorted(value.items())) if isinstance(value, dict) else sorted(value)
        return out


@dataclass
class Finding:
    code: str
    message: str
    blocking: bool
    step: int


@dataclass
class Step:
    argv: list
    verb: str
    kind: str
    destroyed: Destroyed

    def to_dict(self) -> dict:
        return {"argv": self.argv, "verb": self.verb, "kind": self.kind, "files_destroyed": len(self.destroyed.paths)}


@dataclass
class Resolution:
    command: str
    cwd: str
    steps: list
    findings: list
    destroyed: Destroyed
    forced_verdict: str | None
    reasons: list

    def to_dict(self) -> dict:
        return {"command": self.command, "cwd": self.cwd, "forced_verdict": self.forced_verdict,
                "reasons": self.reasons, "steps": [s.to_dict() for s in self.steps],
                "findings": [{"code": f.code, "message": f.message, "blocking": f.blocking} for f in self.findings],
                "destroyed": self.destroyed.to_dict()}
