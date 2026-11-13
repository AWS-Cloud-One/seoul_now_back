"""Owned question projection, expiring actions, atomic admission bindings."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.models import ChatAction, ChatRun
from app.modules.chat_v2.contracts import DialogueProposal
from app.modules.chat_v2.presentation_contracts import ActionView, QuestionDefinition, QuestionView
from app.modules.chat_v2.questions import pending_ids, question_definition
from app.modules.chat_v2.storage_contracts import StorageRejected, StoredContext
from app.repositories.recommendation_conditions import RecommendationConditionsRepository


class InteractionStore:
    async def publish_question(self, db, conversation, run, response):
        if not response.clarification:
            return
        state = StoredContext.model_validate(conversation.context)
        definition = question_definition(state, response.clarification.required_field)
        if definition is None:
            raise ValueError("An exposed question must have persisted continuation context")
        # Preserve the stable question ID and choices, while showing the actual current prompt.
        definition.prompt = response.clarification.question
        run.question = definition.model_dump(mode="json")
        await self.ensure_actions(db, conversation, run, definition)
        self.event(
            conversation,
            run,
            "clarification_required",
            clarification_id=definition.clarification_id,
        )

    async def ensure_actions(self, db, conversation, run, definition):
        if not definition.choices or conversation.active_run_id is not None:
            return
        now = self.clock()
        existing = await db.scalar(
            select(ChatAction.action_id)
            .where(
                ChatAction.source_run_id == run.run_id,
                ChatAction.state_version == conversation.state_version,
                ChatAction.expires_at > now,
            )
            .limit(1)
        )
        if existing:
            return
        current = await RecommendationConditionsRepository(db).get_current(conversation.trip_id)
        for choice in definition.choices:
            # The same runtime contract validates both server choices and model proposals.
            decision = DialogueProposal(decision=choice.decision).decision.model_dump(mode="json")
            db.add(
                ChatAction(
                    action_id=uuid4(),
                    conversation_id=conversation.conversation_id,
                    source_run_id=run.run_id,
                    clarification_id=definition.clarification_id,
                    key=choice.key,
                    label=choice.label,
                    description=choice.description,
                    decision=decision,
                    condition_version=current.version,
                    state_version=conversation.state_version,
                    created_at=now,
                    expires_at=min(now + timedelta(minutes=5), conversation.expires_at),
                )
            )
        await db.flush()

    async def question_view(self, db, conversation, run):
        if run.question is None:
            return None
        definition = QuestionDefinition.model_validate(run.question)
        state = StoredContext.model_validate(conversation.context)
        active = definition.clarification_id in pending_ids(state)
        actions = list(
            await db.scalars(
                select(ChatAction)
                .where(
                    ChatAction.source_run_id == run.run_id,
                )
                .order_by(
                    ChatAction.created_at.desc(), ChatAction.state_version.desc(), ChatAction.key
                )
            )
        )
        # A refresh issues a fresh set; historical tokens remain for replay and expiry errors.
        if actions:
            generation = actions[0].created_at, actions[0].state_version
            actions = [a for a in actions if (a.created_at, a.state_version) == generation]
        options = []
        definitions = {c.key: c for c in definition.choices}
        for action in actions:
            choice = definitions[action.key]
            status = (
                "consumed"
                if action.consumed_by_run_id
                else "stale"
                if not active or action.state_version != conversation.state_version
                else "expired"
                if action.expires_at <= self.clock()
                else "busy"
                if conversation.active_run_id is not None
                else "available"
            )
            options.append(
                ActionView(
                    action_id=action.action_id,
                    label=action.label,
                    description=action.description,
                    target_ref=choice.target_ref,
                    collection_id=choice.collection_id,
                    expires_at=action.expires_at,
                    expected_condition_version=action.condition_version,
                    expected_state_version=action.state_version,
                    status=status,
                    consumed_by_run_id=action.consumed_by_run_id,
                )
            )
        return QuestionView(
            **definition.model_dump(exclude={"choices"}),
            source_run_id=run.run_id,
            status="active" if active else "resolved",
            options=options,
        )

    async def current_questions(self, db, conversation):
        state = StoredContext.model_validate(conversation.context)
        result = []
        for question_id in sorted(pending_ids(state)):
            run = await db.scalar(
                select(ChatRun)
                .where(
                    ChatRun.conversation_id == conversation.conversation_id,
                    ChatRun.question["clarification_id"].as_string() == question_id,
                )
                .order_by(ChatRun.sequence.desc())
                .limit(1)
            )
            if run is not None:
                definition = QuestionDefinition.model_validate(run.question)
                await self.ensure_actions(db, conversation, run, definition)
                result.append(await self.question_view(db, conversation, run))
        return result

    async def owned_action(self, db, conversation, action_id):
        action = await db.scalar(
            select(ChatAction)
            .where(
                ChatAction.action_id == action_id,
                ChatAction.conversation_id == conversation.conversation_id,
            )
            .with_for_update()
        )
        if action is None:
            raise StorageRejected("NOT_FOUND")
        return action

    def validate_action(self, action, conversation, current, request):
        if action.expires_at <= self.clock():
            raise StorageRejected("ACTION_EXPIRED")
        state = StoredContext.model_validate(conversation.context)
        if (
            action.state_version != conversation.state_version
            or action.condition_version != current.version
            or request.expected_state_version != action.state_version
            or request.expected_condition_version != action.condition_version
            or action.clarification_id not in pending_ids(state)
        ):
            raise StorageRejected("ACTION_STALE")
        return {
            "schema_version": 1,
            "kind": "action",
            "decision": action.decision,
            "label": action.label,
            "clarification_id": action.clarification_id,
        }
