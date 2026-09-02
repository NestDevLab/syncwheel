"""Agentwheel revision-provider protocol and crash-safe state machine.

The protocol layer is deliberately independent from Syncwheel's Git and
manifest implementation.  ``syncwheel.py`` supplies an in-process backend so
the provider can reuse the canonical branch, replay, ledger, and validation
primitives without invoking the CLI recursively.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, ContextManager, Protocol, TextIO


PROTOCOL_VERSION = 1
PROVIDER_ID = "syncwheel"
SUPPORTED_ACTIONS = frozenset({"check", "preflight", "finalize", "recover", "release"})
MUTATING_ACTIONS = frozenset({"finalize", "recover"})
PHASES = (
    "prepared",
    "product_committed",
    "stack_owned",
    "control_committed",
    "verified",
)
TERMINAL_PHASES = frozenset({*PHASES, "expired"})
REQUEST_FIELDS = frozenset(
    {
        "protocolVersion",
        "action",
        "operationId",
        "repositoryRoot",
        "expectedHead",
        "expectedManifestDigest",
        "commandName",
        "reason",
        "noCommit",
        "paths",
    }
)
REQUIRED_REQUEST_FIELDS = REQUEST_FIELDS - {"expectedManifestDigest"}
PATH_FIELDS = frozenset({"path", "beforeSha256", "afterSha256"})
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
REASON_CONTROL_CHARACTER = re.compile(r"[\x00\x08\x0b\x0c\x0e-\x1f\x7f]")
ERROR_UTF16_LIMIT = 4095


class RevisionProviderError(Exception):
    """A bounded, user-actionable provider rejection."""


def _utf16_length(value: str) -> int:
    """Return JavaScript-compatible string length without accepting surrogates."""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _wire_text(value: Any, limit: int) -> str:
    """Return scalar text bounded in UTF-16 code units for Agentwheel/Zod."""
    try:
        source = str(value)
    except Exception:
        source = "provider rejected request"
    output: list[str] = []
    used = 0
    for character in source:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            character = "\ufffd"
            units = 1
        else:
            units = 2 if codepoint > 0xFFFF else 1
        if used + units > limit:
            break
        output.append(character)
        used += units
    bounded = "".join(output)
    return bounded or "provider rejected request"


@dataclass(frozen=True)
class PathIntent:
    path: str
    before_sha256: str | None
    after_sha256: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "beforeSha256": self.before_sha256,
            "afterSha256": self.after_sha256,
        }


@dataclass(frozen=True)
class RevisionRequest:
    action: str
    operation_id: str
    repository_root: str
    expected_head: str
    expected_manifest_digest: str | None
    command_name: str
    reason: str
    no_commit: bool
    paths: tuple[PathIntent, ...]

    @property
    def draft_stack_id(self) -> str:
        return f"agentwheel-{self.operation_id}"

    @property
    def draft_branch(self) -> str:
        return f"syncwheel/draft/{self.draft_stack_id}"

    def intent_json(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "operationId": self.operation_id,
            "repositoryRoot": self.repository_root,
            "expectedHead": self.expected_head,
            "expectedManifestDigest": self.expected_manifest_digest,
            "commandName": self.command_name,
            "reason": self.reason,
            "noCommit": self.no_commit,
            "paths": [item.as_json() for item in self.paths],
        }

    @property
    def plan_digest(self) -> str:
        encoded = json.dumps(
            self.intent_json(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class RevisionBackend(Protocol):
    """Narrow facade implemented by the Syncwheel runtime."""

    def operation_lock(self, request: RevisionRequest) -> ContextManager[None]: ...

    def load_journal(self, request: RevisionRequest) -> dict[str, Any] | None: ...

    def save_journal(self, request: RevisionRequest, journal: dict[str, Any]) -> None: ...

    def delete_journal(self, request: RevisionRequest) -> None: ...

    def check(self, request: RevisionRequest) -> dict[str, Any]: ...

    def preflight(self, request: RevisionRequest) -> dict[str, Any]: ...

    def verify_after_paths(self, request: RevisionRequest) -> None: ...

    def prepare_product_commit(
        self, request: RevisionRequest, message: str
    ) -> dict[str, Any] | None: ...

    def prepare_draft_projection(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> dict[str, Any]: ...

    def validate_prepared_commit(
        self, request: RevisionRequest, journal: dict[str, Any], kind: str
    ) -> None: ...

    def ensure_draft_ref_owned(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> None: ...

    def current_head(self, request: RevisionRequest) -> str: ...

    def publish_prepared_commit(
        self, request: RevisionRequest, commit: str, expected_parent: str
    ) -> str: ...

    def align_index(self, request: RevisionRequest, commit: str) -> str: ...

    def ensure_stack_owned(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> dict[str, Any]: ...

    def prepare_control_commit(
        self, request: RevisionRequest, message: str
    ) -> str | None: ...

    def verify_final(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> dict[str, Any]: ...

    def verify_derived_final(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> dict[str, Any]: ...

    def verify_no_repository_delta(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> dict[str, Any]: ...

    def recover_owned_index_lock(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> None: ...

    def verify_recovery_gate(
        self, request: RevisionRequest, journal: dict[str, Any]
    ) -> None: ...

    def verify_release(self, request: RevisionRequest, journal: dict[str, Any]) -> None: ...

    def expire_manifest_invalidated(
        self,
        request: RevisionRequest,
        journal: dict[str, Any],
        reason: str,
    ) -> None: ...

    def checkpoint(self, phase: str) -> None: ...


def _require_string(
    value: Any,
    field: str,
    *,
    nonempty: bool = True,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        qualifier = "a non-empty string" if nonempty else "a string"
        raise RevisionProviderError(f"{field} must be {qualifier}")
    if "\x00" in value:
        raise RevisionProviderError(f"{field} must not contain NUL")
    if max_length is not None and _utf16_length(value) > max_length:
        raise RevisionProviderError(f"{field} must be at most {max_length} characters")
    return value


def _parse_hash(value: Any, field: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not HEX_64.fullmatch(value):
        suffix = " or null" if nullable else ""
        raise RevisionProviderError(f"{field} must be a lowercase SHA-256{suffix}")
    return value


def _parse_path(value: Any, index: int) -> str:
    path = _require_string(value, f"paths[{index}].path", max_length=4096)
    if path.startswith("/") or "\\" in path:
        raise RevisionProviderError(f"paths[{index}].path must be repo-relative POSIX syntax")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RevisionProviderError(f"paths[{index}].path is not normalized")
    if parts[0] == ".git":
        raise RevisionProviderError(f"paths[{index}].path must not address Git internals")
    if path in {".syncwheel/manifest.json", ".syncwheel/ledger"} or path.startswith(
        ".syncwheel/ledger/"
    ):
        raise RevisionProviderError(
            f"paths[{index}].path is Syncwheel-owned control state, not product state"
        )
    return path


def parse_request(payload: Any) -> RevisionRequest:
    if not isinstance(payload, dict):
        raise RevisionProviderError("request must be a JSON object")
    unknown = sorted(set(payload) - REQUEST_FIELDS)
    if unknown:
        raise RevisionProviderError("unknown request field(s): " + ", ".join(unknown))
    missing = sorted(REQUIRED_REQUEST_FIELDS - set(payload))
    if missing:
        raise RevisionProviderError("missing request field(s): " + ", ".join(missing))

    version = payload["protocolVersion"]
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise RevisionProviderError(
            f"unsupported protocolVersion: {version!r}; expected {PROTOCOL_VERSION}"
        )
    action = _require_string(payload["action"], "action")
    if action not in SUPPORTED_ACTIONS:
        raise RevisionProviderError(f"unsupported action: {action}")
    operation_id = _require_string(payload["operationId"], "operationId")
    if not OPERATION_ID.fullmatch(operation_id):
        raise RevisionProviderError(
            "operationId must be 1-63 ref-safe characters using letters, digits, '_' or '-'"
        )
    repository_root = _require_string(payload["repositoryRoot"], "repositoryRoot")
    if not repository_root.startswith("/"):
        raise RevisionProviderError("repositoryRoot must be an absolute path")
    expected_head = _require_string(payload["expectedHead"], "expectedHead")
    if not HEX_40.fullmatch(expected_head):
        raise RevisionProviderError("expectedHead must be a lowercase full Git SHA")
    expected_manifest_digest = None
    if "expectedManifestDigest" in payload:
        expected_manifest_digest = _parse_hash(
            payload["expectedManifestDigest"], "expectedManifestDigest", nullable=False
        )
    command_name = _require_string(
        payload["commandName"], "commandName", max_length=256
    )
    if "\n" in command_name or "\r" in command_name:
        raise RevisionProviderError("commandName must be a single line")
    reason = _require_string(payload["reason"], "reason", max_length=4096)
    if REASON_CONTROL_CHARACTER.search(reason):
        raise RevisionProviderError("reason contains an unsupported control character")
    no_commit = payload["noCommit"]
    if not isinstance(no_commit, bool):
        raise RevisionProviderError("noCommit must be a boolean")
    raw_paths = payload["paths"]
    if not isinstance(raw_paths, list):
        raise RevisionProviderError("paths must be an array")
    parsed_paths: list[PathIntent] = []
    seen_paths: set[str] = set()
    for index, raw_path in enumerate(raw_paths):
        if not isinstance(raw_path, dict):
            raise RevisionProviderError(f"paths[{index}] must be an object")
        unknown_path_fields = sorted(set(raw_path) - PATH_FIELDS)
        if unknown_path_fields:
            raise RevisionProviderError(
                f"unknown paths[{index}] field(s): " + ", ".join(unknown_path_fields)
            )
        missing_path_fields = sorted(PATH_FIELDS - set(raw_path))
        if missing_path_fields:
            raise RevisionProviderError(
                f"missing paths[{index}] field(s): " + ", ".join(missing_path_fields)
            )
        path = _parse_path(raw_path["path"], index)
        if path in seen_paths:
            raise RevisionProviderError(f"duplicate path: {path}")
        seen_paths.add(path)
        before_sha256 = _parse_hash(
            raw_path["beforeSha256"], f"paths[{index}].beforeSha256", nullable=True
        )
        after_sha256 = _parse_hash(
            raw_path["afterSha256"], f"paths[{index}].afterSha256", nullable=True
        )
        if before_sha256 == after_sha256:
            raise RevisionProviderError(
                f"paths[{index}] beforeSha256 and afterSha256 must differ; "
                "protocol v1 cannot represent a mode-only change because it has no mode lease"
            )
        parsed_paths.append(
            PathIntent(
                path=path,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
            )
        )
    parsed_paths.sort(key=lambda item: item.path)
    return RevisionRequest(
        action=action,
        operation_id=operation_id,
        repository_root=repository_root,
        expected_head=expected_head,
        expected_manifest_digest=expected_manifest_digest,
        command_name=command_name,
        reason=reason,
        no_commit=no_commit,
        paths=tuple(parsed_paths),
    )


def product_commit_message(request: RevisionRequest) -> str:
    return (
        f"agentwheel: {request.command_name}\n\n"
        f"{request.reason.rstrip()}\n\n"
        f"Agentwheel-Operation: {request.operation_id}\n"
    )


def control_commit_message(request: RevisionRequest) -> str:
    return (
        f"syncwheel: own Agentwheel operation {request.operation_id}\n\n"
        f"{request.reason.rstrip()}\n\n"
        f"Agentwheel-Operation: {request.operation_id}\n"
    )


def _base_response(action: str, operation_id: str, ok: bool, status: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "providerId": PROVIDER_ID,
        "action": action,
        "operationId": operation_id,
        "ok": ok,
        "status": status,
    }


def _mutation_response(
    request: RevisionRequest,
    journal: dict[str, Any],
    *,
    status: str | None = None,
) -> dict[str, Any]:
    phase = journal.get("phase", "prepared")
    ownership = _ownership_response_fields(journal)
    response = _base_response(request.action, request.operation_id, True, status or phase)
    response.update(
        {
            "expectedHead": journal.get("expectedHead", request.expected_head),
            "resultingHead": journal.get("resultingHead", request.expected_head),
            "productCommitSha": journal.get("productCommitSha"),
            **ownership,
            "manifestDigest": journal.get("manifestDigest"),
            "unmappedIntegrationCommits": list(
                journal.get("unmappedIntegrationCommits") or []
            ),
            "published": False,
        }
    )
    return response


def _ownership_response_fields(journal: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "draftStackId": journal.get("draftStackId"),
        "draftBranch": journal.get("draftBranch"),
        "draftTipSha": journal.get("candidateDraftCommitSha"),
        "controlCommitSha": journal.get("controlCommitSha"),
    }
    if not all(fields.values()):
        return {field: None for field in fields}
    return fields


def _valid_sha(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _error_action(raw: Any) -> str:
    value = raw.get("action") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value or _utf16_length(value) > 80:
        return "unknown"
    return _wire_text(value, 80)


def _error_operation_id(raw: Any) -> str:
    value = raw.get("operationId") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value or _utf16_length(value) > 128:
        return "unknown"
    return _wire_text(value, 128)


def _error_ownership_fields(
    operation_id: str, journal: dict[str, Any]
) -> dict[str, Any]:
    empty = {
        "draftStackId": None,
        "draftBranch": None,
        "draftTipSha": None,
        "controlCommitSha": None,
    }
    if not OPERATION_ID.fullmatch(operation_id):
        return empty
    stack_id = journal.get("draftStackId")
    branch = journal.get("draftBranch")
    tip = _valid_sha(journal.get("candidateDraftCommitSha"), HEX_40)
    control = _valid_sha(journal.get("controlCommitSha"), HEX_40)
    expected_stack = f"agentwheel-{operation_id}"
    expected_branch = f"syncwheel/draft/{expected_stack}"
    if stack_id != expected_stack or branch != expected_branch or not tip or not control:
        return empty
    return {
        "draftStackId": stack_id,
        "draftBranch": branch,
        "draftTipSha": tip,
        "controlCommitSha": control,
    }


def _valid_unmapped(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        sha = _valid_sha(item, HEX_40)
        if sha is not None and sha not in seen:
            seen.add(sha)
            result.append(sha)
    return result


def _error_response(
    raw: Any,
    message: str,
    journal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = _error_action(raw)
    operation_id = _error_operation_id(raw)
    response = _base_response(action, operation_id, False, "rejected")
    response["error"] = _wire_text(message, ERROR_UTF16_LIMIT)
    if action in MUTATING_ACTIONS:
        journal = journal or {}
        ownership = _error_ownership_fields(operation_id, journal)
        raw_expected = raw.get("expectedHead") if isinstance(raw, dict) else None
        response.update(
            {
                "expectedHead": _valid_sha(journal.get("expectedHead"), HEX_40)
                or _valid_sha(raw_expected, HEX_40),
                "resultingHead": _valid_sha(journal.get("resultingHead"), HEX_40),
                "productCommitSha": _valid_sha(
                    journal.get("productCommitSha"), HEX_40
                ),
                **ownership,
                "manifestDigest": _valid_sha(journal.get("manifestDigest"), HEX_64),
                "unmappedIntegrationCommits": _valid_unmapped(
                    journal.get("unmappedIntegrationCommits")
                ),
                "published": False,
            }
        )
    return response


def _new_journal(request: RevisionRequest, observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "providerId": PROVIDER_ID,
        "operationId": request.operation_id,
        "planDigest": request.plan_digest,
        "request": request.intent_json(),
        "phase": "prepared",
        "expectedHead": request.expected_head,
        "resultingHead": request.expected_head,
        "observedManifestDigest": observation["manifestDigest"],
        "manifestDigest": observation["manifestDigest"],
        "baselineWorktrees": observation["worktrees"],
        "baselineRemoteRefs": observation["remoteRefs"],
        "managedLocalRefs": observation["managedLocalRefs"],
        "refTransactionRefs": observation["refTransactionRefs"],
        "integrationBranch": observation["integrationBranch"],
        "baseRef": observation["baseRef"],
        "baseRefFullName": observation["baseRefFullName"],
        "baseRefSha": observation["baseRefSha"],
        "baseRefObjectSha": observation["baseRefObjectSha"],
        "baseRefObservation": observation["baseRefObservation"],
        "projectionBaseSha": observation["projectionBaseSha"],
        "projectionBaseKind": observation["projectionBaseKind"],
        "integrationCompositionDigest": observation.get("integrationCompositionDigest"),
        "projectionRoute": None,
        "baselineIndexSha256": observation["indexSha256"],
        "productIndexSha256": None,
        "controlIndexSha256": None,
        "indexAlignments": {},
        "coordination": observation.get("coordination"),
        "candidateProductCommitSha": None,
        "candidateProductTreeSha": None,
        "productPathObjects": None,
        "candidateDraftCommitSha": None,
        "candidateDraftTreeSha": None,
        "productHooksValidated": False,
        "draftRefOwned": False,
        "productCommitSha": None,
        "draftStackId": None,
        "draftBranch": None,
        "candidateControlCommitSha": None,
        "candidateControlTreeSha": None,
        "controlPathObjects": None,
        "controlHooksValidated": False,
        "manifestReplaced": False,
        "ledgerAppended": False,
        "controlCommitSha": None,
        "unmappedIntegrationCommits": list(observation["unmappedIntegrationCommits"]),
        "published": False,
        "expiration": None,
    }


def _assert_matching_journal(
    request: RevisionRequest, journal: dict[str, Any] | None
) -> dict[str, Any]:
    if journal is None:
        raise RevisionProviderError(
            f"operation {request.operation_id} has no prepared journal; run preflight first"
        )
    if journal.get("providerId") != PROVIDER_ID or journal.get("schemaVersion") != 1:
        raise RevisionProviderError(f"operation {request.operation_id} has an incompatible journal")
    if journal.get("planDigest") != request.plan_digest:
        raise RevisionProviderError(
            f"operationId collision for {request.operation_id}: intent digest differs"
        )
    phase = journal.get("phase")
    if phase not in TERMINAL_PHASES:
        raise RevisionProviderError(
            f"operation {request.operation_id} has an unknown journal phase: {phase!r}"
        )
    return journal


def _expired_message(request: RevisionRequest, journal: dict[str, Any]) -> str:
    expiration = journal.get("expiration") or {}
    reason = expiration.get("reason") or "the operation receipt was invalidated"
    remedy = expiration.get("remedy") or "run a new Agentwheel update"
    return f"operation {request.operation_id} expired: {reason}; {remedy}"


def _persist_phase(
    backend: RevisionBackend,
    request: RevisionRequest,
    journal: dict[str, Any],
    phase: str,
) -> None:
    journal["phase"] = phase
    backend.save_journal(request, journal)
    backend.checkpoint(phase)


def _advance(
    backend: RevisionBackend,
    request: RevisionRequest,
    journal: dict[str, Any],
) -> dict[str, Any]:
    backend.verify_recovery_gate(request, journal)
    if journal["phase"] == "expired":
        backend.expire_manifest_invalidated(
            request, journal,
            (journal.get("expiration") or {}).get("reason")
            or "the operation receipt was invalidated",
        )
    if journal["phase"] == "verified":
        status = journal.get("terminalStatus") or "verified"
        return _mutation_response(request, journal, status=status)
    backend.recover_owned_index_lock(request, journal)

    if request.no_commit:
        if journal["phase"] == "prepared":
            backend.verify_after_paths(request)
        journal.update(
            {
                "phase": "verified",
                "terminalStatus": "revisioning-skipped",
                "resultingHead": backend.current_head(request),
            }
        )
        backend.verify_recovery_gate(request, journal)
        backend.save_journal(request, journal)
        backend.checkpoint("verified")
        backend.verify_recovery_gate(request, journal)
        return _mutation_response(request, journal, status="revisioning-skipped")

    if journal["phase"] == "prepared":
        candidate = journal.get("candidateProductCommitSha")
        current = backend.current_head(request)
        if candidate is None and current != request.expected_head:
            raise RevisionProviderError(
                "integration HEAD changed before product object preparation; refusing recovery"
            )
        if candidate is None:
            backend.verify_after_paths(request)
            prepared = backend.prepare_product_commit(
                request, product_commit_message(request)
            )
            if prepared is None:
                verified = backend.verify_no_repository_delta(request, journal)
                journal.update(verified)
                journal["phase"] = "verified"
                journal["terminalStatus"] = "no-repository-delta"
                backend.verify_recovery_gate(request, journal)
                backend.save_journal(request, journal)
                backend.checkpoint("verified")
                backend.verify_recovery_gate(request, journal)
                return _mutation_response(request, journal, status="no-repository-delta")
            journal.update(prepared)
            candidate = journal["candidateProductCommitSha"]
            backend.save_journal(request, journal)
            backend.checkpoint("product_objects_prepared")

        if journal.get("candidateDraftCommitSha") is None:
            projection = backend.prepare_draft_projection(request, journal)
            journal.update(projection)
            candidate = journal["candidateProductCommitSha"]
            backend.save_journal(request, journal)
            backend.checkpoint("route_decided")

        if not journal.get("productHooksValidated"):
            backend.validate_prepared_commit(request, journal, "product")
            journal["productHooksValidated"] = True
            backend.save_journal(request, journal)
            backend.checkpoint("product_hooks_validated")

        if journal.get("projectionRoute") != "derived" and not journal.get("draftRefOwned"):
            backend.ensure_draft_ref_owned(request, journal)
            journal["draftRefOwned"] = True
            journal["draftStackId"] = request.draft_stack_id
            journal["draftBranch"] = request.draft_branch
            backend.save_journal(request, journal)
            backend.checkpoint("draft_ref_owned")

        current = backend.current_head(request)
        if current in {request.expected_head, candidate}:
            backend.publish_prepared_commit(request, candidate, request.expected_head)
        else:
            raise RevisionProviderError(
                "integration HEAD changed before product commit publication; refusing recovery"
            )
        journal["productIndexSha256"] = backend.align_index(request, candidate)
        journal["productCommitSha"] = candidate
        journal["resultingHead"] = candidate
        backend.save_journal(request, journal)
        backend.checkpoint("integration_product_committed")
        _persist_phase(backend, request, journal, "product_committed")

    if journal["phase"] == "product_committed":
        if journal.get("projectionRoute") == "derived":
            verified = backend.verify_derived_final(request, journal)
            journal.update(verified)
            journal["terminalStatus"] = "verified"
            journal["publicationState"] = "derived-unpublished"
            _persist_phase(backend, request, journal, "verified")
            return _mutation_response(request, journal, status="verified")
        ownership = backend.ensure_stack_owned(request, journal)
        journal.update(ownership)
        journal["draftStackId"] = request.draft_stack_id
        journal["draftBranch"] = request.draft_branch
        _persist_phase(backend, request, journal, "stack_owned")

    if journal["phase"] == "stack_owned":
        candidate = journal.get("candidateControlCommitSha")
        if candidate is None:
            prepared = backend.prepare_control_commit(
                request, control_commit_message(request)
            )
            if prepared is None:
                raise RevisionProviderError(
                    "stack ownership produced no manifest-only control delta"
                )
            journal.update(prepared)
            candidate = journal["candidateControlCommitSha"]
            backend.save_journal(request, journal)
            backend.checkpoint("control_objects_prepared")
        if not journal.get("controlHooksValidated"):
            backend.validate_prepared_commit(request, journal, "control")
            journal["controlHooksValidated"] = True
            backend.save_journal(request, journal)
            backend.checkpoint("control_hooks_validated")
        product = journal["productCommitSha"]
        current = backend.current_head(request)
        if current in {product, candidate}:
            backend.publish_prepared_commit(request, candidate, product)
        else:
            raise RevisionProviderError(
                "integration HEAD changed before control commit publication; refusing recovery"
            )
        journal["controlIndexSha256"] = backend.align_index(request, candidate)
        journal["controlCommitSha"] = candidate
        journal["resultingHead"] = candidate
        backend.save_journal(request, journal)
        backend.checkpoint("integration_control_committed")
        _persist_phase(backend, request, journal, "control_committed")

    if journal["phase"] == "control_committed":
        verified = backend.verify_final(request, journal)
        journal.update(verified)
        journal["terminalStatus"] = "verified"
        journal["publicationState"] = "owned-but-unpublished"
        _persist_phase(backend, request, journal, "verified")

    backend.verify_recovery_gate(request, journal)
    return _mutation_response(
        request, journal, status=journal.get("terminalStatus") or journal["phase"]
    )


def handle_request(backend: RevisionBackend, request: RevisionRequest) -> dict[str, Any]:
    if request.action == "check":
        backend.check(request)
        return _base_response(request.action, request.operation_id, True, "ready")

    with backend.operation_lock(request):
        existing = backend.load_journal(request)
        if request.action == "preflight":
            if existing is not None:
                existing = _assert_matching_journal(request, existing)
                if existing["phase"] == "expired":
                    raise RevisionProviderError(_expired_message(request, existing))
                backend.verify_recovery_gate(request, existing)
                return _base_response(
                    request.action, request.operation_id, True, "prepared"
                )
            observation = backend.preflight(request)
            journal = _new_journal(request, observation)
            backend.save_journal(request, journal)
            backend.checkpoint("prepared")
            return _base_response(request.action, request.operation_id, True, "prepared")

        journal = _assert_matching_journal(request, existing)
        if request.action in MUTATING_ACTIONS:
            return _advance(backend, request, journal)
        if request.action == "release":
            if journal["phase"] == "expired":
                return _base_response(request.action, request.operation_id, True, "expired")
            if journal["phase"] != "prepared":
                raise RevisionProviderError(
                    f"cannot release operation in phase {journal['phase']}; recover it instead"
                )
            backend.verify_recovery_gate(request, journal)
            backend.verify_release(request, journal)
            backend.verify_recovery_gate(request, journal)
            backend.delete_journal(request)
            return _base_response(request.action, request.operation_id, True, "released")
    raise RevisionProviderError(f"unsupported action: {request.action}")


def run_provider_stream(
    backend: RevisionBackend,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    raw: Any = None
    journal: dict[str, Any] | None = None
    try:
        document = stdin.read()
        if not document.strip():
            raise RevisionProviderError("request body is empty")
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as exc:
            raise RevisionProviderError(f"invalid request JSON: {exc.msg}") from exc
        request = parse_request(raw)
        try:
            response = handle_request(backend, request)
        except Exception:
            try:
                journal = backend.load_journal(request)
            except Exception:
                journal = None
            raise
        stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except RevisionProviderError as exc:
        message = str(exc)
    except Exception as exc:  # Fail closed without leaking a traceback into the wire response.
        message = f"provider failure: {exc}"
    response = _error_response(raw, message, journal)
    stderr.write(response["error"] + "\n")
    stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    return 2
