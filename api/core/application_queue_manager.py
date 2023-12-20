import queue
from typing import Generator

from sqlalchemy.orm import DeclarativeMeta

from core.entities.application_entities import InvokeFrom
from core.entities.queue_entities import QueueStopEvent, AppQueueEvent, QueuePingEvent, QueueErrorEvent, \
    QueueAgentThoughtEvent, QueueMessageEndEvent, QueueRetrieverResourcesEvent, QueueMessageReplaceEvent, \
    QueueMessageEvent, QueueMessage
from core.model_runtime.entities.llm_entities import LLMResult, LLMResultChunk
from extensions.ext_redis import redis_client
from models.model import MessageAgentThought


class ApplicationQueueManager:
    def __init__(self, task_id: str,
                 user_id: str,
                 invoke_from: InvokeFrom,
                 conversation_id: str,
                 app_mode: str,
                 message_id: str) -> None:
        if not user_id:
            raise ValueError("user is required")

        self._task_id = task_id
        self._user_id = user_id
        self._invoke_from = invoke_from
        self._conversation_id = str(conversation_id)
        self._app_mode = app_mode
        self._message_id = str(message_id)

        user_prefix = 'account' if self._invoke_from in [InvokeFrom.EXPLORE, InvokeFrom.DEBUGGER] else 'end-user'
        redis_client.setex(self._generate_task_belong_cache_key(self._task_id), 1800, f"{user_prefix}-{self._user_id}")

        q = queue.Queue()

        self._q = q

    def listen(self) -> Generator:
        """
        Listen to queue
        :return:
        """
        # wait for 10 minutes to stop listen
        listen_timeout = 600

        while True:
            try:
                message = self._q.get(timeout=1)
                if message is None:
                    break

                yield message
            except queue.Empty:
                continue
            finally:
                listen_timeout -= 1
                if listen_timeout <= 0 or self._is_stopped():
                    # publish two messages to make sure the client can receive the stop signal
                    # and stop listening after the stop signal processed
                    self.publish(QueueStopEvent())
                    self.stop_listen()

                if listen_timeout > 0 and listen_timeout % 10 == 0:
                    self.publish(QueuePingEvent())

    def stop_listen(self) -> None:
        """
        Stop listen to queue
        :return:
        """
        self._q.put(None)

    def publish_chunk_message(self, chunk: LLMResultChunk) -> None:
        """
        Publish chunk message to channel

        :param chunk: chunk
        :return:
        """
        self.publish(QueueMessageEvent(
            chunk=chunk
        ))

    def publish_message_replace(self, text: str) -> None:
        """
        Publish message replace
        :param text: text
        :return:
        """
        self.publish(QueueMessageReplaceEvent(
            text=text
        ))

    def publish_retriever_resources(self, retriever_resources: list[dict]) -> None:
        """
        Publish retriever resources
        :return:
        """
        self.publish(QueueRetrieverResourcesEvent(retriever_resources=retriever_resources))

    def publish_message_end(self, llm_result: LLMResult) -> None:
        """
        Publish message end
        :param llm_result: llm result
        :return:
        """
        self.publish(QueueMessageEndEvent(llm_result=llm_result))
        self.stop_listen()

    def publish_agent_thought(self, message_agent_thought: MessageAgentThought) -> None:
        """
        Publish agent thought
        :param message_agent_thought: message agent thought
        :return:
        """
        self.publish(QueueAgentThoughtEvent(
            agent_thought_id=message_agent_thought.id
        ))

    def publish_error(self, e) -> None:
        """
        Publish error
        :param e: error
        :return:
        """
        self.publish(QueueErrorEvent(
            error=e
        ))
        self.stop_listen()

    def publish(self, event: AppQueueEvent) -> None:
        """
        Publish event to queue
        :param event:
        :return:
        """
        self._check_for_sqlalchemy_models(event)

        message = QueueMessage(
            task_id=self._task_id,
            message_id=self._message_id,
            conversation_id=self._conversation_id,
            app_mode=self._app_mode,
            event=event
        )

        self._q.put(message)

        if isinstance(event, QueueStopEvent):
            raise ConversationTaskStoppedException()

    def set_stop_flag(self) -> None:
        """
        Set task stop flag
        :return:
        """
        result = redis_client.get(self._generate_task_belong_cache_key(self._task_id))
        if result is None:
            return

        user_prefix = 'account' if self._invoke_from in [InvokeFrom.EXPLORE, InvokeFrom.DEBUGGER] else 'end-user'
        if result != f"{user_prefix}-{self._user_id}":
            return

        stopped_cache_key = self._generate_stopped_cache_key(self._task_id)
        redis_client.setex(stopped_cache_key, 600, 1)

    def _is_stopped(self) -> bool:
        """
        Check if task is stopped
        :return:
        """
        stopped_cache_key = self._generate_stopped_cache_key(self._task_id)
        result = redis_client.get(stopped_cache_key)
        if result is not None:
            redis_client.delete(stopped_cache_key)
            return True

        return False

    def _generate_task_belong_cache_key(self, task_id: str) -> str:
        """
        Generate task belong cache key
        :param task_id: task id
        :return:
        """
        return f"generate_task_belong:{task_id}"

    def _generate_stopped_cache_key(self, task_id: str) -> str:
        """
        Generate stopped cache key
        :param task_id: task id
        :return:
        """
        return f"generate_task_stopped:{task_id}"

    def _check_for_sqlalchemy_models(self, event: AppQueueEvent):
        # from entity to dict or list
        data = event.dict()
        if isinstance(data, dict):
            for key, value in data.items():
                self._check_for_sqlalchemy_models(value)
        elif isinstance(data, list):
            for item in data:
                self._check_for_sqlalchemy_models(item)
        else:
            if isinstance(data, DeclarativeMeta) or hasattr(data, '_sa_instance_state'):
                raise TypeError("Critical Error: Passing SQLAlchemy Model instances "
                                "that cause thread safety issues is not allowed.")


class ConversationTaskStoppedException(Exception):
    pass
