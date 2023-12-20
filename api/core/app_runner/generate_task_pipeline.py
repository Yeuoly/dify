import json
import logging
import threading
import time
from decimal import Decimal
from typing import Union, Generator, cast

from pydantic import BaseModel

from core.entities.application_entities import ApplicationGenerateEntity
from core.application_queue_manager import ApplicationQueueManager
from core.entities.queue_entities import QueueErrorEvent, QueueStopEvent, QueueMessageEndEvent, QueueMessage, \
    QueueRetrieverResourcesEvent, QueueAgentThoughtEvent, QueuePingEvent, QueueMessageEvent, QueueMessageReplaceEvent
from core.model_runtime.entities.llm_entities import LLMResult, LLMUsage
from core.model_runtime.entities.message_entities import AssistantPromptMessage, PromptMessageRole, \
    TextPromptMessageContent, PromptMessageContentType, ImagePromptMessageContent, PromptMessage
from core.model_runtime.errors.invoke import InvokeError, InvokeAuthorizationError
from core.model_runtime.model_providers.__base.large_language_model import LargeLanguageModel
from core.prompt.prompt_template import PromptTemplateParser
from events.message_event import message_was_created
from extensions.ext_database import db
from models.model import Message, Conversation, MessageAgentThought

logger = logging.getLogger(__name__)


class TaskState(BaseModel):
    """
    TaskState entity
    """
    llm_result: LLMResult
    metadata: dict = {}


class GenerateTaskPipeline:
    """
    GenerateTaskPipeline is a class that generate stream output and state management for Application.
    """

    def __init__(self, application_generate_entity: ApplicationGenerateEntity,
                 queue_manager: ApplicationQueueManager,
                 conversation: Conversation,
                 message: Message) -> None:
        """
        Initialize GenerateTaskPipeline.
        :param application_generate_entity: application generate entity
        :param queue_manager: queue manager
        :param conversation: conversation
        :param message: message
        """
        self._application_generate_entity = application_generate_entity
        self._queue_manager = queue_manager
        self._conversation = conversation
        self._message = message
        self._task_state = TaskState(
            llm_result=LLMResult(
                model=self._application_generate_entity.app_orchestration_config_entity.model_config.model,
                prompt_messages=[],
                message=AssistantPromptMessage(content=""),
                usage=LLMUsage(
                    prompt_tokens=0,
                    prompt_unit_price=Decimal('0.0'),
                    prompt_price_unit=Decimal('0.0'),
                    prompt_price=Decimal('0.0'),
                    completion_tokens=0,
                    completion_unit_price=Decimal('0.0'),
                    completion_price_unit=Decimal('0.0'),
                    completion_price=Decimal('0.0'),
                    total_tokens=0,
                    total_price=Decimal('0.0'),
                    currency="",
                    latency=.0
                )
            )
        )
        self._start_at = time.perf_counter()

    def process(self, stream: bool) -> Union[dict, Generator]:
        """
        Process generate task pipeline.
        :return:
        """
        if stream:
            return self._process_stream_response()
        else:
            return self._process_blocking_response()

    def _process_blocking_response(self) -> dict:
        """
        Process blocking response.
        :return:
        """
        for queue_message in self._queue_manager.listen():
            event = queue_message.event

            if isinstance(event, QueueErrorEvent):
                raise self._handle_error(event)
            elif isinstance(event, QueueRetrieverResourcesEvent):
                self._task_state.metadata['retriever_resources'] = event.retriever_resources
            elif isinstance(event, QueueMessageEndEvent):
                # Save message
                self._save_message(event.llm_result)

                response = {
                    'event': 'message',
                    'task_id': self._application_generate_entity.task_id,
                    'id': self._message.id,
                    'mode': self._conversation.mode,
                    'answer': event.llm_result.message.content,
                    'metadata': {},
                    'created_at': int(self._message.created_at)
                }

                if self._conversation.mode == 'chat':
                    response['conversation_id'] = self._conversation.id

                if self._task_state.metadata:
                    response['data']['metadata'] = self._task_state.metadata

                return response
            else:
                continue

    def _process_stream_response(self) -> Generator:
        """
        Process stream response.
        :return:
        """
        for message in self._queue_manager.listen():
            event = message.event

            if isinstance(event, QueueErrorEvent):
                raise self._handle_error(event)
            elif isinstance(event, (QueueStopEvent, QueueMessageEndEvent)):
                if isinstance(event, QueueMessageEndEvent):
                    self._task_state.llm_result = event.llm_result
                else:
                    model_config = self._application_generate_entity.app_orchestration_config_entity.model_config
                    model = model_config.model
                    model_instance = model_config.provider_model_bundle.model_instance
                    model_instance = cast(LargeLanguageModel, model_instance)

                    # calculate num tokens
                    prompt_tokens = model_instance.get_num_tokens(model, self._task_state.llm_result.prompt_messages)
                    completion_tokens = model_instance.get_num_tokens(
                        model,
                        [self._task_state.llm_result.message]
                    )

                    credentials = model_config.credentials

                    # transform usage
                    self._task_state.llm_result.usage = model_instance._calc_response_usage(
                        model,
                        credentials,
                        prompt_tokens,
                        completion_tokens
                    )

                # Save message
                self._save_message(self._task_state.llm_result)

                response = {
                    'event': 'message_end',
                    'task_id': self._application_generate_entity.task_id,
                    'id': self._message.id,
                }

                if self._conversation.mode == 'chat':
                    response['conversation_id'] = self._conversation.id

                if self._task_state.metadata:
                    response['metadata'] = self._task_state.metadata

                yield self._yield_response(response)
            elif isinstance(event, QueueRetrieverResourcesEvent):
                self._task_state.metadata['retriever_resources'] = event.retriever_resources
            elif isinstance(event, QueueAgentThoughtEvent):
                agent_thought = (
                    db.session.query(MessageAgentThought)
                    .filter(MessageAgentThought.id == event.agent_thought_id)
                    .first()
                )

                if agent_thought:
                    response = {
                        'event': 'agent_thought',
                        'id': agent_thought.id,
                        'task_id': self._application_generate_entity.task_id,
                        'message_id': self._message.id,
                        'position': agent_thought.position,
                        'thought': agent_thought.thought,
                        'tool': agent_thought.tool,
                        'tool_input': agent_thought.tool_input,
                        'created_at': int(self._message.created_at)
                    }

                    if self._conversation.mode == 'chat':
                        response['conversation_id'] = self._conversation.id

                    yield self._yield_response(response)
            elif isinstance(event, QueueMessageEvent):
                chunk = event.chunk
                delta_text = chunk.delta.message.content
                if delta_text is None:
                    continue

                if not self._task_state.llm_result.prompt_messages:
                    self._task_state.llm_result.prompt_messages = chunk.prompt_messages

                self._task_state.llm_result.message.content += delta_text
                response = self._handle_chunk(delta_text)
                yield self._yield_response(response)
            elif isinstance(event, QueueMessageReplaceEvent):
                response = {
                    'event': 'message_replace',
                    'task_id': self._application_generate_entity.task_id,
                    'message_id': self._message.id,
                    'answer': event.text,
                    'created_at': int(self._message.created_at)
                }

                if self._conversation.mode == 'chat':
                    response['conversation_id'] = self._conversation.id

                yield self._yield_response(response)
            elif isinstance(event, QueuePingEvent):
                yield "event: ping\n\n"
            else:
                continue

    def _save_message(self, llm_result: LLMResult) -> None:
        """
        Save message.
        :param llm_result: llm result
        :return:
        """
        usage = llm_result.usage

        self._message.message = self._prompt_messages_to_prompt_for_saving(self._task_state.llm_result.prompt_messages)
        self._message.message_tokens = usage.prompt_tokens
        self._message.message_unit_price = usage.prompt_unit_price
        self._message.message_price_unit = usage.prompt_price_unit
        self._message.answer = PromptTemplateParser.remove_template_variables(llm_result.message.content.strip()) \
            if llm_result.message.content else ''
        self._message.answer_tokens = usage.completion_tokens
        self._message.answer_unit_price = usage.completion_unit_price
        self._message.answer_price_unit = usage.completion_price_unit
        self._message.provider_response_latency = time.perf_counter() - self._start_at
        self._message.total_price = usage.total_price

        db.session.commit()

        message_was_created.send(
            self._message,
            conversation=self._conversation,
            is_first_message=self._application_generate_entity.conversation_id is None,
            extras=self._application_generate_entity.extras
        )

    def _handle_chunk(self, text: str) -> dict:
        """
        Handle completed event.
        :param text: text
        :return:
        """
        response = {
            'event': 'message',
            'task_id': self._application_generate_entity.task_id,
            'message_id': self._message.id,
            'answer': text,
            'created_at': int(self._message.created_at)
        }

        if self._conversation.mode == 'chat':
            response['conversation_id'] = self._conversation.id

        return response

    def _handle_error(self, event: QueueErrorEvent) -> Exception:
        """
        Handle error event.
        :param event: event
        :return:
        """
        logger.debug("error: %s", event.error)
        e = event.error

        if isinstance(e, InvokeAuthorizationError):
            return InvokeAuthorizationError('Incorrect API key provided')
        elif isinstance(e, InvokeError) or isinstance(e, ValueError):
            return e
        else:
            return Exception(e.description if getattr(e, 'description', None) is not None else str(e))

    def _yield_response(self, response: dict) -> Generator:
        """
        Yield response.
        :param response: response
        :return:
        """
        yield "data: " + json.dumps(response) + "\n\n"

    def _prompt_messages_to_prompt_for_saving(self, prompt_messages: list[PromptMessage]) -> list[dict]:
        """
        Prompt messages to prompt for saving.
        :param prompt_messages: prompt messages
        :return:
        """
        prompts = []
        if self._application_generate_entity.app_orchestration_config_entity.model_config.mode == 'chat':
            for prompt_message in prompt_messages:
                if prompt_message.role == PromptMessageRole.USER:
                    role = 'user'
                elif prompt_message.role == PromptMessageRole.ASSISTANT:
                    role = 'assistant'
                elif prompt_message.role == PromptMessageRole.SYSTEM:
                    role = 'system'
                else:
                    continue

                text = ''
                files = []
                if isinstance(prompt_message.content, list):
                    for content in prompt_message.content:
                        if content.type == PromptMessageContentType.TEXT:
                            content = cast(TextPromptMessageContent, content)
                            text += content.data
                        else:
                            content = cast(ImagePromptMessageContent, content)
                            files.append({
                                "type": 'image',
                                "data": content.data[:10] + '...[TRUNCATED]...' + content.data[-10:],
                                "detail": content.detail.value
                            })
                else:
                    text = prompt_message.content

                prompts.append({
                    "role": role,
                    "text": text,
                    "files": files
                })
        else:
            prompts.append({
                "role": 'user',
                "text": prompt_messages[0].content
            })

        return prompts
