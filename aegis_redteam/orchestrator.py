"""Runs a campaign: every seed x mutation budget, against one target."""
from __future__ import annotations

import uuid
from typing import List, Optional

from .attacks import Seed, CATEGORY_WEIGHT
from .attacker import Attacker
from .gcp_logging import ATTACK_SEVERITY_TO_LOG_LEVEL, get_logger, log
from .judge import Judge
from .llm import ChatMessage
from .store import Finding, Store
from .target import Target

_logger = get_logger(__name__)


class Orchestrator:
    def __init__(self, target: Target, attacker: Attacker, judge: Judge, store: Store, max_turns: int = 3):
        self.target = target
        self.attacker = attacker
        self.judge = judge
        self.store = store
        self.max_turns = max_turns

    def run_campaign(self, seeds: List[Seed], run_id: Optional[str] = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:8]
        log(_logger, "INFO", "campaign_started", run_id=run_id, seed_count=len(seeds), max_turns=self.max_turns)

        violated_count = 0
        for seed in seeds:
            if self._run_seed(seed, run_id):
                violated_count += 1

        log(
            _logger, "INFO", "campaign_finished",
            run_id=run_id, seed_count=len(seeds), violated_count=violated_count,
            success_rate_pct=round(violated_count / len(seeds) * 100, 1) if seeds else 0,
        )
        return run_id

    def _run_seed(self, seed: Seed, run_id: str) -> bool:
        """Returns True if a violation was confirmed (i.e. the pen test failed)."""
        history: List[ChatMessage] = []
        last_response = None
        weight = CATEGORY_WEIGHT.get(seed.category, 2)

        for turn in range(self.max_turns):
            prompt = self.attacker.mutate(seed.prompt, last_response, turn)
            history.append(ChatMessage(role="user", content=prompt))
            response = self.target.respond(history)
            history.append(ChatMessage(role="assistant", content=response))
            last_response = response

            verdict = self.judge.score(seed.category, prompt, response, weight)
            log(
                _logger, "DEBUG" if not verdict.violated else "INFO",
                "attack_turn_scored",
                run_id=run_id, seed_id=seed.id, category=seed.category, turn=turn,
                violated=verdict.violated, attack_severity=verdict.severity,
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
                log(
                    _logger, ATTACK_SEVERITY_TO_LOG_LEVEL.get(verdict.severity, "WARNING"),
                    "finding_confirmed",
                    event="finding_confirmed", run_id=run_id, seed_id=seed.id,
                    category=seed.category, goal=seed.goal, attack_severity=verdict.severity,
                    turns_to_success=turn + 1, rationale=verdict.rationale,
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
        log(
            _logger, "INFO", "seed_defended",
            event="seed_defended", run_id=run_id, seed_id=seed.id, category=seed.category,
        )
        return False
