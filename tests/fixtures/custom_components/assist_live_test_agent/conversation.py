from homeassistant.components import conversation
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import callback
from homeassistant.helpers import intent

from . import ACTIVE_RECORD, DEVICE_ID

REPLIES = {
    "short": "The requested test action is complete.",
    "long": (
        "The requested test action is complete. "
        "This is the fixed long response from the isolated virtual test agent."
    ),
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [TestAgent("short", entry.runtime_data), TestAgent("long", entry.runtime_data)]
    )


class TestAgent(conversation.ConversationEntity):
    _attr_supported_languages = ["en"]

    def __init__(self, group, device_id=DEVICE_ID):
        self._virtual_device_id = device_id
        self._reply_group = group
        self._attr_name = f"Assist live {group}"
        self._attr_unique_id = f"assist_live_test_{group}"
        self._attr_supports_streaming = group == "long"

    @property
    def supported_languages(self):
        return ["en"]

    @property
    def extra_state_attributes(self):
        return {"virtual_device_id": self._virtual_device_id}

    async def _async_handle_message(self, user_input, chat_log):
        record = {
            "recognized_transcript": user_input.text,
            "context_id": user_input.context.id,
            "actions": [],
            "service_calls": [],
            "group": self._reply_group,
            "text_deltas": [],
        }

        @callback
        def service_event(event):
            if event.context.id == user_input.context.id:
                call = {
                    "domain": event.data["domain"],
                    "service": event.data["service"],
                    "service_data": event.data["service_data"],
                    "context_id": event.context.id,
                }
                record["service_calls"].append(call)
                record["actions"].append({"kind": "service", **call})

        unsub = self.hass.bus.async_listen(EVENT_CALL_SERVICE, service_event)
        token = ACTIVE_RECORD.set(record)
        try:
            result = await conversation.async_converse(
                self.hass,
                user_input.text,
                None,
                user_input.context,
                language=user_input.language,
                agent_id=conversation.HOME_ASSISTANT_AGENT,
                device_id=user_input.device_id,
                satellite_id=user_input.satellite_id,
            )
        finally:
            ACTIVE_RECORD.reset(token)
            unsub()
        selected = result.response.intent
        record["selected_intent"] = (
            {
                "name": selected.intent_type,
                "slots": {key: value["value"] for key, value in selected.slots.items()},
            }
            if selected
            else None
        )
        record["response_type"] = result.response.response_type.value
        if (
            selected is not None
            and selected.intent_type == "HassStartTimer"
            and result.response.response_type == intent.IntentResponseType.ACTION_DONE
            and "conversation_command" in selected.slots
        ):
            record["actions"].append(
                {
                    "kind": "delayed_action",
                    "event": "scheduled",
                    "command": selected.slots["conversation_command"]["value"],
                    "seconds": sum(
                        int(selected.slots.get(key, {}).get("value", 0)) * factor
                        for key, factor in (
                            ("hours", 3600),
                            ("minutes", 60),
                            ("seconds", 1),
                        )
                    ),
                }
            )
        reply = REPLIES[self._reply_group]
        if result.response.response_type == intent.IntentResponseType.ERROR:
            reply = result.response.speech.get("plain", {}).get("speech", "")
        record["reply"] = reply
        if self._reply_group == "long" and selected is not None:

            async def deltas():
                for index, part in enumerate((reply[:45], reply[45:])):
                    record["text_deltas"].append(part)
                    yield (
                        {"role": "assistant", "content": part}
                        if index == 0
                        else {"content": part}
                    )

            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id, deltas()
            ):
                pass
        else:
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(agent_id=self.entity_id, content=reply)
            )
        result.response.async_set_speech(reply)
        self.hass.bus.async_fire(
            "assist_live_test_record", record, context=user_input.context
        )
        return conversation.ConversationResult(
            response=result.response, conversation_id=chat_log.conversation_id
        )
