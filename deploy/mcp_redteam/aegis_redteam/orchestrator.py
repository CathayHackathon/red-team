"""Runs a campaign: every seed x mutation budget, against one target.

Every meaningful action -- which seed/category/turn is running, the exact
mutated prompt the attacker sent, the target's response, the judge's verdict
and rationale, and which LLM backend/model/region produced them -- is both
(a) written to Cloud Logging as a structured JSON line via `gcp_logging.log`
(so `jsonPayload.run_id="..."` in Logs Explorer shows the whole campaign live
while it runs) and (b) handed to an optional `on_event` callback, which the
Cloud Run HTTP handler uses to stream the same events back to the caller's
terminal in near-real-time (see `deploy/mcp_redteam/app.py`'s `?stream=1`
mode). Both destinations get identical detail from one code path.
"""
from __future__ import annotations

import uuid
from typing import Callable, Dict, List, Optional

from .attacks import Seed, CATEGORY_WEIGHT
from .attacker import Attacker
from .gcp_logging import ATTACK_SEVERITY_TO_LOG_LEVEL, get_logger, log
from .judge import Judge
from .llm import ChatMessage
from .store import Finding, Store
from .target import Target

_logger = get_logger(__name__)

# Cap how much prompt/response/rationale text lands in any single log line or
# streamed event -- enough to see exactly what technique/wording was used,
# without ballooning Cloud Logging volume or the streamed response size.
_SNIPPET_LEN = 500


def _snippet(text: Optional[str], limit: int = _SNIPPET_LEN) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _backend_info(backend) -> Dict[str, str]:
    """Best-effort parameter dump for whatever backend is in play, so every
    campaign_started event/log line answers "what is it actually calling"
    (backend flavor, model name, Vertex region/project) without the caller
    needing to separately inspect env vars."""
    info = {"backend": getattr(backend, "name", type(backend).__name__)}
    for attr in ("model", "region", "project"):
        val = getattr(backend, attr, None)
        if val:
            info[attr] = val
    return info


class Orchestrator:
    def __init__(
        self,
        target: Target,
        attacker: Attacker,
        judge: Judge,
        store: Store,
        max_turns: int = 3,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.target = target
        self.attacker = attacker
        self.judge = judge
        self.store = store
        self.max_turns = max_turns
        self.on_event = on_event

    def _emit(self, level: str, event: str, **fields) -> None:
        log(_logger, level, event, event=event, **fields)
        if self.on_event is not None:
            try:
                self.on_event({"event": event, "severity": level, **fields})
            except Exception:
                # A broken/disconnected stream consumer must never take down
                # the campaign itself -- Cloud Logging already has the line.
                pass

    def run_campaign(self, seeds: List[Seed], run_id: Optional[str] = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:8]
        backend_info = _backend_info(self.attacker.backend)
        self._emit(
            "INFO", "campaign_started",
            run_id=run_id, seed_count=len(seeds), max_turns=self.max_turns,
            **backend_info,
        )

        violated_count = 0
        for seed in seeds:
            if self._run_seed(seed, run_id):
                violated_count += 1

        self._emit(
            "INFO", "campaign_finished",
            run_id=run_id, seed_count=len(seeds), violated_count=violated_count,
            success_rate_pct=round(violated_count / len(seeds) * 100, 1) if seeds else 0,
        )
        return run_id

    def _run_seed(self, seed: Seed, run_id: str) -> bool:
        """Returns True if a violation was confirmed (i.e. the pen test failed)."""
        history: List[ChatMessage] = []
        last_response = None
        weight = CATEGORY_WEIGHT.get(seed.category, 2)

        self._emit(
            "INFO", "seed_started",
            run_id=run_id, seed_id=seed.id, category=seed.category, goal=seed.goal,
        )

        for turn in range(self.max_turns):
            prompt = self.attacker.mutate(seed.prompt, last_response, turn)
            self._emit(
                "INFO", "attacker_mutated",
                run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                prompt=_snippet(prompt),
            )
            history.append(ChatMessage(role="user", content=prompt))

            response = self.target.respond(history)
            self._emit(
                "INFO", "target_responded",
                run_id=run_id, seed_id=seed.id, turn=turn,
                response=_snippet(response),
            )
            history.append(ChatMessage(role="assistant", content=response))
            last_response = response

            verdict = self.judge.score(seed.category, prompt, response, weight)
            self._emit(
                "DEBUG" if not verdict.violated else "INFO",
                "judge_scored",
                run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                violated=verdict.violated, attack_severity=verdict.severity,
                rationale=_snippet(verdict.rationale),
            )

            if verdict.violated:
                self.store.write(
                    Finding(
                        run_id=run_id,
                        seed_id=seed.id,
                        category=seed.category,
                        goal=seed.goal,
                        violated=True,
                        severity=verdict.severity,
                        rationale=verdict.rationale,
                        turns_to_success=turn + 1,
                        transcript=[{"role": m.role, "content": m.content} for m in history],
                    )
                )
                self._emit(
                    ATTACK_SEVERITY_TO_LOG_LEVEL.get(verdict.severity, "WARNING"),
                    "finding_confirmed",
                    run_id=run_id, seed_id=seed.id,
                    category=seed.category, goal=seed.goal, attack_severity=verdict.severity,
                    turns_to_success=turn + 1, rationale=_snippet(verdict.rationale),
                )
                return True

        # No success within budget -> record a clean (non-violating) result too,
        # so the report can show attempted-but-defended seeds, not just hits.
        self.store.write(
            Finding(
                run_id=run_id,
                seed_id=seed.id,
                category=seed.category,
                goal=seed.goal,
                violated=False,
                severity=0,
                rationale="No violation found within turn budget.",
                turns_to_success=None,
                transcript=[{"role": m.role, "content": m.content} for m in history],
            )
        )
        self._emit(
            "INFO", "seed_defended",
            run_id=run_id, seed_id=seed.id, category=seed.category,
        )
        return False
