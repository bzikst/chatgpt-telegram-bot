from __future__ import annotations
import datetime
import logging
import os

import tiktoken

import openai

import json
import httpx
import io
from PIL import Image

from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from utils import is_direct_result, encode_image, decode_image
from plugin_manager import PluginManager

# Models can be found here: https://platform.openai.com/docs/models/overview
# Models gpt-3.5-turbo-0613 and  gpt-3.5-turbo-16k-0613 will be deprecated on June 13, 2024
GPT_3_MODELS = ("gpt-3.5-turbo", "gpt-3.5-turbo-0301", "gpt-3.5-turbo-0613")
GPT_3_16K_MODELS = ("gpt-3.5-turbo-16k", "gpt-3.5-turbo-16k-0613", "gpt-3.5-turbo-1106", "gpt-3.5-turbo-0125")
GPT_4_MODELS = ("gpt-4", "gpt-4-0314", "gpt-4-0613", "gpt-4-turbo-preview")
GPT_4_32K_MODELS = ("gpt-4-32k", "gpt-4-32k-0314", "gpt-4-32k-0613")
GPT_4_VISION_MODELS = ("gpt-4o",)
GPT_4_128K_MODELS = ("gpt-4-1106-preview", "gpt-4-0125-preview", "gpt-4-turbo-preview", "gpt-4-turbo", "gpt-4-turbo-2024-04-09")
GPT_4O_MODELS = ("gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest")
GPT_4_1_MODELS = ("gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano")
O_MODELS = ("o1", "o1-mini", "o1-preview")
GPT_ALL_MODELS = GPT_3_MODELS + GPT_3_16K_MODELS + GPT_4_MODELS + GPT_4_32K_MODELS + GPT_4_VISION_MODELS + GPT_4_128K_MODELS + GPT_4O_MODELS + GPT_4_1_MODELS + O_MODELS

def default_max_tokens(model: str) -> int:
    """
    Gets the default number of max tokens for the given model.
    :param model: The model name
    :return: The default number of max tokens
    """
    base = 1200
    if model in GPT_3_MODELS:
        return base
    elif model in GPT_4_MODELS:
        return base * 2
    elif model in GPT_3_16K_MODELS:
        if model == "gpt-3.5-turbo-1106":
            return 4096
        return base * 4
    elif model in GPT_4_32K_MODELS:
        return base * 8
    elif model in GPT_4_VISION_MODELS:
        return 4096
    elif model in GPT_4_128K_MODELS:
        return 4096
    elif model in GPT_4O_MODELS:
        return 4096
    elif model in GPT_4_1_MODELS:
        return 30_000
    elif model in O_MODELS:
        return 4096


def are_functions_available(model: str) -> bool:
    """
    Whether the given model supports functions
    """
    if model in ("gpt-3.5-turbo-0301", "gpt-4-0314", "gpt-4-32k-0314", "gpt-3.5-turbo-0613", "gpt-3.5-turbo-16k-0613"):
        return False
    if model in O_MODELS:
        return False
    return True


# Load translations
parent_dir_path = os.path.join(os.path.dirname(__file__), os.pardir)
translations_file_path = os.path.join(parent_dir_path, 'translations.json')
with open(translations_file_path, 'r', encoding='utf-8') as f:
    translations = json.load(f)


def localized_text(key, bot_language):
    """
    Return translated text for a key in specified bot_language.
    Keys and translations can be found in the translations.json.
    """
    try:
        return translations[bot_language][key]
    except KeyError:
        logging.warning(f"No translation available for bot_language code '{bot_language}' and key '{key}'")
        # Fallback to English if the translation is not available
        if key in translations['en']:
            return translations['en'][key]
        else:
            logging.warning(f"No english definition found for key '{key}' in translations.json")
            # return key as text
            return key


class OpenAIHelper:
    """
    ChatGPT helper class.
    """

    def __init__(self, config: dict, plugin_manager: PluginManager):
        """
        Initializes the OpenAI helper class with the given configuration.
        :param config: A dictionary containing the GPT configuration
        :param plugin_manager: The plugin manager
        """
        http_client = httpx.AsyncClient(proxies=config['proxy']) if config.get('proxy') is not None else None
        self.client = openai.AsyncOpenAI(api_key=config['api_key'], http_client=http_client)
        self.config = config
        self.plugin_manager = plugin_manager
        self.conversations: dict[int: list] = {}  # {chat_id: history}
        self.conversations_vision: dict[int: bool] = {}  # {chat_id: is_vision}
        self.last_updated: dict[int: datetime] = {}  # {chat_id: last_update_timestamp}

    def __to_responses_message(self, role: str, content) -> dict | None:
        """
        Convert an existing chat message (role, content) into Responses API input message.
        Skips unsupported roles (like function) for now.
        """
        if role == 'function':
            return None
        blocks = []
        if isinstance(content, str):
            block_type = 'input_text' if role in ('user', 'system', 'developer') else 'output_text'
            blocks.append({'type': block_type, 'text': content})
        else:
            for part in content:
                ptype = part.get('type')
                if ptype == 'text':
                    block_type = 'input_text' if role in ('user', 'system', 'developer') else 'output_text'
                    blocks.append({'type': block_type, 'text': part.get('text', '')})
                elif ptype == 'image_url':
                    # convert to input_image; assistant images are rare, treat as input_image for simplicity
                    blocks.append({'type': 'input_image', 'image_url': part.get('image_url', {})})
                else:
                    # passthrough unknown
                    blocks.append(part)
        return {'role': role, 'content': blocks}

    def get_conversation_stats(self, chat_id: int) -> tuple[int, int]:
        """
        Gets the number of messages and tokens used in the conversation.
        :param chat_id: The chat ID
        :return: A tuple containing the number of messages and tokens used
        """
        if chat_id not in self.conversations:
            self.reset_chat_history(chat_id)
        return len(self.conversations[chat_id]), self.__count_tokens(self.conversations[chat_id])

    async def get_chat_response(self, chat_id: int, query: str) -> tuple[str, str]:
        """
        Gets a full response from the GPT model.
        :param chat_id: The chat ID
        :param query: The query to send to the model
        :return: The answer from the model and the number of tokens used
        """
        plugins_used = ()
        response = await self.__common_get_chat_response(chat_id, query)
        if not self.config.get('use_responses_api', False):
            if self.config['enable_functions'] and not self.conversations_vision[chat_id]:
                response, plugins_used = await self.__handle_function_call(chat_id, response)
                if is_direct_result(response):
                    return response, '0'

            answer = ''

            if len(response.choices) > 1 and self.config['n_choices'] > 1:
                for index, choice in enumerate(response.choices):
                    content = choice.message.content.strip()
                    if index == 0:
                        self.__add_to_history(chat_id, role="assistant", content=content)
                    answer += f'{index + 1}\u20e3\n'
                    answer += content
                    answer += '\n\n'
            else:
                answer = response.choices[0].message.content.strip()
                self.__add_to_history(chat_id, role="assistant", content=answer)

            bot_language = self.config['bot_language']
            show_plugins_used = len(plugins_used) > 0 and self.config['show_plugins_used']
            plugin_names = tuple(self.plugin_manager.get_plugin_source_name(plugin) for plugin in plugins_used)
            if self.config['show_usage']:
                answer += "\n\n---\n" \
                          f"💰 {str(response.usage.total_tokens)} {localized_text('stats_tokens', bot_language)}" \
                          f" ({str(response.usage.prompt_tokens)} {localized_text('prompt', bot_language)}," \
                          f" {str(response.usage.completion_tokens)} {localized_text('completion', bot_language)})"
                if show_plugins_used:
                    answer += f"\n🔌 {', '.join(plugin_names)}"
            elif show_plugins_used:
                answer += f"\n\n---\n🔌 {', '.join(plugin_names)}"

            return answer, response.usage.total_tokens
        else:
            # Responses API path
            answer = response.output_text.strip() if hasattr(response, 'output_text') and response.output_text else ''
            if answer:
                self.__add_to_history(chat_id, role="assistant", content=answer)
            usage_total = getattr(response, 'usage', None).total_tokens if getattr(response, 'usage', None) else self.__count_tokens(self.conversations[chat_id])
            if self.config['show_usage']:
                bot_language = self.config['bot_language']
                answer += "\n\n---\n" \
                          f"💰 {str(usage_total)} {localized_text('stats_tokens', bot_language)}"
            return answer, str(usage_total)

    async def get_chat_response_stream(self, chat_id: int, query: str):
        """
        Stream response from the GPT model.
        :param chat_id: The chat ID
        :param query: The query to send to the model
        :return: The answer from the model and the number of tokens used, or 'not_finished'
        """
        plugins_used = ()
        if not self.config.get('use_responses_api', False):
            response = await self.__common_get_chat_response(chat_id, query, stream=True)
            if self.config['enable_functions'] and not self.conversations_vision[chat_id]:
                response, plugins_used = await self.__handle_function_call(chat_id, response, stream=True)
                if is_direct_result(response):
                    yield response, '0'
                    return

            answer = ''
            async for chunk in response:
                if len(chunk.choices) == 0:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    answer += delta.content
                    yield answer, 'not_finished'
            answer = answer.strip()
            self.__add_to_history(chat_id, role="assistant", content=answer)
            tokens_used = str(self.__count_tokens(self.conversations[chat_id]))

            show_plugins_used = len(plugins_used) > 0 and self.config['show_plugins_used']
            plugin_names = tuple(self.plugin_manager.get_plugin_source_name(plugin) for plugin in plugins_used)
            if self.config['show_usage']:
                answer += f"\n\n---\n💰 {tokens_used} {localized_text('stats_tokens', self.config['bot_language'])}"
                if show_plugins_used:
                    answer += f"\n🔌 {', '.join(plugin_names)}"
            elif show_plugins_used:
                answer += f"\n\n---\n🔌 {', '.join(plugin_names)}"

            yield answer, tokens_used
        else:
            # Responses API streaming
            # Ensure conversation exists and is fresh
            if chat_id not in self.conversations or self.__max_age_reached(chat_id):
                self.reset_chat_history(chat_id)
            self.last_updated[chat_id] = datetime.datetime.now()
            self.__add_to_history(chat_id, role="user", content=query)

            # Optionally summarise if too long
            try:
                token_count = self.__count_tokens(self.conversations[chat_id])
                exceeded_max_tokens = token_count + self.config['max_tokens'] > self.__max_model_tokens()
                exceeded_max_history_size = len(self.conversations[chat_id]) > self.config['max_history_size']
                if exceeded_max_tokens or exceeded_max_history_size:
                    logging.info(f'Chat history for chat ID {chat_id} is too long. Summarising...')
                    try:
                        summary = await self.__summarise(self.conversations[chat_id][:-1])
                        self.reset_chat_history(chat_id, self.conversations[chat_id][0]['content'])
                        self.__add_to_history(chat_id, role="assistant", content=summary)
                        self.__add_to_history(chat_id, role="user", content=query)
                    except Exception as e:
                        logging.warning(f'Error while summarising chat history: {str(e)}. Popping elements instead...')
                        self.conversations[chat_id] = self.conversations[chat_id][-self.config['max_history_size']:]
            except Exception:
                pass

            model_to_use = self.config['model']
            # Build input from history with proper block types
            input_messages = []
            for m in self.conversations[chat_id]:
                converted = self.__to_responses_message(m['role'], m['content'])
                if converted:
                    input_messages.append(converted)
            tools = []
            use_web_search = self.config.get('enable_web_search', False) and not self.conversations_vision.get(chat_id, False)
            if use_web_search:
                tools.append({'type': 'web_search'})

            answer = ''
            try:
                # Prepend a formatting guard when using web search to avoid Markdown
                if use_web_search:
                    formatting_text = (
                        'When using web search or tools, format strictly for Telegram: NO Markdown or HTML. '
                        'Return plain text only. Do not use *, _, `, [], (), or any markup. '
                        'If you must show a URL, print the raw URL.'
                    )
                    input_messages = [{'role': 'system', 'content': [{'type': 'input_text', 'text': formatting_text}]}] + input_messages

                async with self.client.responses.stream(
                    model=model_to_use,
                    input=input_messages,
                    temperature=self.config['temperature'],
                    max_output_tokens=self.config['max_tokens'],
                    tools=tools
                ) as stream:
                    async for event in stream:
                        et = getattr(event, 'type', None)
                        if et == 'response.output_text.delta':
                            delta = getattr(event, 'delta', '')
                            if delta:
                                answer += delta
                                yield answer, 'not_finished'
                        elif et == 'response.refusal.delta':
                            # ignore/refusal accumulation
                            pass
                    final = await stream.get_final_response()
                    answer = answer.strip()
                    self.__add_to_history(chat_id, role="assistant", content=answer)
                    tokens_used = getattr(getattr(final, 'usage', None), 'total_tokens', None)
                    if tokens_used is None:
                        tokens_used = self.__count_tokens(self.conversations[chat_id])
                    if self.config['show_usage']:
                        answer += f"\n\n---\n💰 {tokens_used} {localized_text('stats_tokens', self.config['bot_language'])}"
                    yield answer, str(tokens_used)
            except Exception as e:
                logging.warning(f'Responses stream failed, falling back to non-stream: {str(e)}')
                full, tokens = await self.get_chat_response(chat_id, query)
                yield full, tokens

    @retry(
        reraise=True,
        retry=retry_if_exception_type(openai.RateLimitError),
        wait=wait_fixed(20),
        stop=stop_after_attempt(3)
    )
    async def __common_get_chat_response(self, chat_id: int, query: str, stream=False):
        """
        Request a response from the GPT model.
        :param chat_id: The chat ID
        :param query: The query to send to the model
        :return: The answer from the model and the number of tokens used
        """
        bot_language = self.config['bot_language']
        try:
            if chat_id not in self.conversations or self.__max_age_reached(chat_id):
                self.reset_chat_history(chat_id)

            self.last_updated[chat_id] = datetime.datetime.now()

            self.__add_to_history(chat_id, role="user", content=query)

            # Summarize the chat history if it's too long to avoid excessive token usage
            token_count = self.__count_tokens(self.conversations[chat_id])
            exceeded_max_tokens = token_count + self.config['max_tokens'] > self.__max_model_tokens()
            exceeded_max_history_size = len(self.conversations[chat_id]) > self.config['max_history_size']

            if exceeded_max_tokens or exceeded_max_history_size:
                logging.info(f'Chat history for chat ID {chat_id} is too long. Summarising...')
                try:
                    summary = await self.__summarise(self.conversations[chat_id][:-1])
                    logging.debug(f'Summary: {summary}')
                    self.reset_chat_history(chat_id, self.conversations[chat_id][0]['content'])
                    self.__add_to_history(chat_id, role="assistant", content=summary)
                    self.__add_to_history(chat_id, role="user", content=query)
                except Exception as e:
                    logging.warning(f'Error while summarising chat history: {str(e)}. Popping elements instead...')
                    self.conversations[chat_id] = self.conversations[chat_id][-self.config['max_history_size']:]

            if not self.config.get('use_responses_api', False):
                max_tokens_str = 'max_completion_tokens' if self.config['model'] in O_MODELS else 'max_tokens'
                common_args = {
                    'model': self.config['model'] if not self.conversations_vision[chat_id] else self.config['vision_model'],
                    'messages': self.conversations[chat_id],
                    'temperature': self.config['temperature'],
                    'n': self.config['n_choices'],
                    max_tokens_str: self.config['max_tokens'],
                    'presence_penalty': self.config['presence_penalty'],
                    'frequency_penalty': self.config['frequency_penalty'],
                    'stream': stream
                }

                if self.config['enable_functions'] and not self.conversations_vision[chat_id]:
                    functions = self.plugin_manager.get_functions_specs()
                    if len(functions) > 0:
                        common_args['functions'] = self.plugin_manager.get_functions_specs()
                        common_args['function_call'] = 'auto'
                return await self.client.chat.completions.create(**common_args)
            else:
                # Responses API with optional tools (web_search and plugins)
                use_web_search = self.config.get('enable_web_search', False) and not self.conversations_vision.get(chat_id, False)
                tools = [{'type': 'web_search'}] if use_web_search else []

                # Include plugin tools as Responses tools if enabled
                responses_tools_plugins = []
                if self.config.get('enable_functions', False):
                    try:
                        # Convert function specs into Responses tool schema (only valid specs)
                        for spec in self.plugin_manager.get_functions_specs() or []:
                            if not isinstance(spec, dict):
                                continue
                            name = spec.get('name')
                            if not name or not isinstance(name, str):
                                continue
                            description = spec.get('description', '') or ''
                            parameters = spec.get('parameters')
                            if not isinstance(parameters, dict):
                                parameters = {'type': 'object', 'properties': {}}
                            responses_tools_plugins.append({
                                'type': 'function',
                                'function': {
                                    'name': name,
                                    'description': description,
                                    'parameters': parameters
                                }
                            })
                    except Exception:
                        pass
                if responses_tools_plugins:
                    tools.extend(responses_tools_plugins)

                model_to_use = self.config['model'] if not self.conversations_vision.get(chat_id, False) else self.config['vision_model']
                # Convert chat history to Responses input messages
                input_messages = []
                for m in self.conversations[chat_id]:
                    if m['role'] == 'assistant':
                        continue
                    converted = self.__to_responses_message(m['role'], m['content'])
                    if converted:
                        input_messages.append(converted)
                # Prepend a formatting guard when using web search to avoid Markdown
                if use_web_search:
                    formatting_text = (
                        'Use only plain text answers, NO Markdown or HTML. '
                        'Return plain text only. Do not use *, _, `, [], (), or any markup. '
                        'If you must show a URL, print the raw URL.'
                    )
                    input_messages = [{'role': 'system', 'content': [{'type': 'input_text', 'text': formatting_text}]}] + input_messages
                # Tool execution loop for Responses (non-stream)
                plugins_used = []
                resp = await self.client.responses.create(
                    model=model_to_use,
                    input=input_messages,
                    temperature=self.config['temperature'],
                    max_output_tokens=self.config['max_tokens'],
                    tools=tools
                )
                # Handle tool calls if any
                max_calls = self.config.get('functions_max_consecutive_calls', 10)
                calls = 0
                while getattr(resp, 'status', None) == 'requires_action' and getattr(getattr(resp, 'required_action', None), 'submit_tool_outputs', None) and calls < max_calls:
                    tool_outputs = []
                    try:
                        for tool in resp.required_action.submit_tool_outputs.tool_calls:
                            if tool.type == 'function' and tool.function:
                                fn_name = tool.function.name
                                fn_args = tool.function.arguments or '{}'
                                result = await self.plugin_manager.call_function(fn_name, self, fn_args)
                                tool_outputs.append({'tool_call_id': tool.id, 'output': result})
                                plugins_used.append(fn_name)
                    except Exception as e:
                        logging.warning(f'Responses tool execution error: {str(e)}')
                        break

                    resp = await self.client.responses.submit_tool_outputs(
                        response_id=resp.id,
                        tool_outputs=tool_outputs
                    )
                    calls += 1

                # Finalize answer
                answer = getattr(resp, 'output_text', '') or ''
                answer = answer.strip()
                if answer:
                    self.__add_to_history(chat_id, role="assistant", content=answer)
                usage_total = getattr(getattr(resp, 'usage', None), 'total_tokens', None)
                if usage_total is None:
                    usage_total = self.__count_tokens(self.conversations[chat_id])
                if self.config['show_usage']:
                    bot_language = self.config['bot_language']
                    answer += "\n\n---\n" \
                              f"💰 {str(usage_total)} {localized_text('stats_tokens', bot_language)}"
                    if plugins_used and self.config.get('show_plugins_used', False):
                        plugin_names = tuple(self.plugin_manager.get_plugin_source_name(p) for p in plugins_used)
                        answer += f"\n🔌 {', '.join(plugin_names)}"
                return answer, str(usage_total)

        except openai.RateLimitError as e:
            raise e

        except openai.BadRequestError as e:
            raise Exception(f"⚠️ _{localized_text('openai_invalid', bot_language)}._ ⚠️\n{str(e)}") from e

        except Exception as e:
            raise Exception(f"⚠️ _{localized_text('error', bot_language)}._ ⚠️\n{str(e)}") from e

    async def __handle_function_call(self, chat_id, response, stream=False, times=0, plugins_used=()):
        function_name = ''
        arguments = ''
        if stream:
            async for item in response:
                if len(item.choices) > 0:
                    first_choice = item.choices[0]
                    if first_choice.delta and first_choice.delta.function_call:
                        if first_choice.delta.function_call.name:
                            function_name += first_choice.delta.function_call.name
                        if first_choice.delta.function_call.arguments:
                            arguments += first_choice.delta.function_call.arguments
                    elif first_choice.finish_reason and first_choice.finish_reason == 'function_call':
                        break
                    else:
                        return response, plugins_used
                else:
                    return response, plugins_used
        else:
            if len(response.choices) > 0:
                first_choice = response.choices[0]
                if first_choice.message.function_call:
                    if first_choice.message.function_call.name:
                        function_name += first_choice.message.function_call.name
                    if first_choice.message.function_call.arguments:
                        arguments += first_choice.message.function_call.arguments
                else:
                    return response, plugins_used
            else:
                return response, plugins_used

        logging.info(f'Calling function {function_name} with arguments {arguments}')
        function_response = await self.plugin_manager.call_function(function_name, self, arguments)

        if function_name not in plugins_used:
            plugins_used += (function_name,)

        if is_direct_result(function_response):
            self.__add_function_call_to_history(chat_id=chat_id, function_name=function_name,
                                                content=json.dumps({'result': 'Done, the content has been sent'
                                                                              'to the user.'}))
            return function_response, plugins_used

        self.__add_function_call_to_history(chat_id=chat_id, function_name=function_name, content=function_response)
        response = await self.client.chat.completions.create(
            model=self.config['model'],
            messages=self.conversations[chat_id],
            functions=self.plugin_manager.get_functions_specs(),
            function_call='auto' if times < self.config['functions_max_consecutive_calls'] else 'none',
            stream=stream
        )
        return await self.__handle_function_call(chat_id, response, stream, times + 1, plugins_used)

    async def generate_image(self, prompt: str) -> tuple[str, str]:
        """
        Generates an image from the given prompt using DALL·E model.
        :param prompt: The prompt to send to the model
        :return: The image URL and the image size
        """
        bot_language = self.config['bot_language']
        try:
            response = await self.client.images.generate(
                prompt=prompt,
                n=1,
                model=self.config['image_model'],
                quality=self.config['image_quality'],
                style=self.config['image_style'],
                size=self.config['image_size']
            )

            if len(response.data) == 0:
                logging.error(f'No response from GPT: {str(response)}')
                raise Exception(
                    f"⚠️ _{localized_text('error', bot_language)}._ "
                    f"⚠️\n{localized_text('try_again', bot_language)}."
                )

            return response.data[0].url, self.config['image_size']
        except Exception as e:
            raise Exception(f"⚠️ _{localized_text('error', bot_language)}._ ⚠️\n{str(e)}") from e

    async def generate_speech(self, text: str) -> tuple[any, int]:
        """
        Generates an audio from the given text using TTS model.
        :param prompt: The text to send to the model
        :return: The audio in bytes and the text size
        """
        bot_language = self.config['bot_language']
        try:
            response = await self.client.audio.speech.create(
                model=self.config['tts_model'],
                voice=self.config['tts_voice'],
                input=text,
                response_format='opus'
            )

            temp_file = io.BytesIO()
            temp_file.write(response.read())
            temp_file.seek(0)
            return temp_file, len(text)
        except Exception as e:
            raise Exception(f"⚠️ _{localized_text('error', bot_language)}._ ⚠️\n{str(e)}") from e

    async def transcribe(self, filename):
        """
        Transcribes the audio file using the Whisper model.
        """
        try:
            with open(filename, "rb") as audio:
                prompt_text = self.config['whisper_prompt']
                result = await self.client.audio.transcriptions.create(model="whisper-1", file=audio, prompt=prompt_text)
                return result.text
        except Exception as e:
            logging.exception(e)
            raise Exception(f"⚠️ _{localized_text('error', self.config['bot_language'])}._ ⚠️\n{str(e)}") from e

    @retry(
        reraise=True,
        retry=retry_if_exception_type(openai.RateLimitError),
        wait=wait_fixed(20),
        stop=stop_after_attempt(3)
    )
    async def __common_get_chat_response_vision(self, chat_id: int, content: list, stream=False):
        """
        Request a response from the GPT model.
        :param chat_id: The chat ID
        :param query: The query to send to the model
        :return: The answer from the model and the number of tokens used
        """
        bot_language = self.config['bot_language']
        try:
            if chat_id not in self.conversations or self.__max_age_reached(chat_id):
                self.reset_chat_history(chat_id)

            self.last_updated[chat_id] = datetime.datetime.now()

            if self.config['enable_vision_follow_up_questions']:
                self.conversations_vision[chat_id] = True
                self.__add_to_history(chat_id, role="user", content=content)
            else:
                for message in content:
                    if message['type'] == 'text':
                        query = message['text']
                        break
                self.__add_to_history(chat_id, role="user", content=query)

            # Summarize the chat history if it's too long to avoid excessive token usage
            token_count = self.__count_tokens(self.conversations[chat_id])
            exceeded_max_tokens = token_count + self.config['max_tokens'] > self.__max_model_tokens()
            exceeded_max_history_size = len(self.conversations[chat_id]) > self.config['max_history_size']

            if exceeded_max_tokens or exceeded_max_history_size:
                logging.info(f'Chat history for chat ID {chat_id} is too long. Summarising...')
                try:
                    
                    last = self.conversations[chat_id][-1]
                    summary = await self.__summarise(self.conversations[chat_id][:-1])
                    logging.debug(f'Summary: {summary}')
                    self.reset_chat_history(chat_id, self.conversations[chat_id][0]['content'])
                    self.__add_to_history(chat_id, role="assistant", content=summary)
                    self.conversations[chat_id] += [last]
                except Exception as e:
                    logging.warning(f'Error while summarising chat history: {str(e)}. Popping elements instead...')
                    self.conversations[chat_id] = self.conversations[chat_id][-self.config['max_history_size']:]

            message = {'role':'user', 'content':content}

            if not self.config.get('use_responses_api', False):
                common_args = {
                    'model': self.config['vision_model'],
                    'messages': self.conversations[chat_id][:-1] + [message],
                    'temperature': self.config['temperature'],
                    'n': 1,
                    'max_tokens': self.config['vision_max_tokens'],
                    'presence_penalty': self.config['presence_penalty'],
                    'frequency_penalty': self.config['frequency_penalty'],
                    'stream': stream
                }
                return await self.client.chat.completions.create(**common_args)
            else:
                # Responses API vision
                input_messages = []
                for m in self.conversations[chat_id][:-1] + [message]:
                    c = m['content']
                    if isinstance(c, str):
                        input_messages.append({'role': m['role'], 'content': [{'type': 'input_text', 'text': c}]})
                    else:
                        # convert old style to Responses content types if necessary
                        converted = []
                        for part in c:
                            if part.get('type') == 'text':
                                converted.append({'type': 'input_text', 'text': part.get('text', '')})
                            elif part.get('type') == 'image_url':
                                converted.append({'type': 'input_image', 'image_url': part.get('image_url', {})})
                            else:
                                converted.append(part)
                        input_messages.append({'role': m['role'], 'content': converted})
                return await self.client.responses.create(
                    model=self.config['vision_model'],
                    input=input_messages,
                    temperature=self.config['temperature'],
                    max_output_tokens=self.config['vision_max_tokens']
                )

        except openai.RateLimitError as e:
            raise e

        except openai.BadRequestError as e:
            raise Exception(f"⚠️ _{localized_text('openai_invalid', bot_language)}._ ⚠️\n{str(e)}") from e

        except Exception as e:
            raise Exception(f"⚠️ _{localized_text('error', bot_language)}._ ⚠️\n{str(e)}") from e


    async def interpret_image(self, chat_id, fileobj, prompt=None):
        """
        Interprets a given PNG image file using the Vision model.
        """
        image = encode_image(fileobj)
        prompt = self.config['vision_prompt'] if prompt is None else prompt

        content = [{'type':'text', 'text':prompt}, {'type':'image_url', \
                    'image_url': {'url':image, 'detail':self.config['vision_detail'] } }]

        response = await self.__common_get_chat_response_vision(chat_id, content)

        

        # functions are not available for this model
        
        # if self.config['enable_functions']:
        #     response, plugins_used = await self.__handle_function_call(chat_id, response)
        #     if is_direct_result(response):
        #         return response, '0'

        answer = ''

        if not self.config.get('use_responses_api', False):
            if len(response.choices) > 1 and self.config['n_choices'] > 1:
                for index, choice in enumerate(response.choices):
                    content = choice.message.content.strip()
                    if index == 0:
                        self.__add_to_history(chat_id, role="assistant", content=content)
                    answer += f'{index + 1}\u20e3\n'
                    answer += content
                    answer += '\n\n'
            else:
                answer = response.choices[0].message.content.strip()
                self.__add_to_history(chat_id, role="assistant", content=answer)
        else:
            answer = response.output_text.strip() if hasattr(response, 'output_text') and response.output_text else ''
            if answer:
                self.__add_to_history(chat_id, role="assistant", content=answer)

        bot_language = self.config['bot_language']
        # Plugins are not enabled either
        # show_plugins_used = len(plugins_used) > 0 and self.config['show_plugins_used']
        # plugin_names = tuple(self.plugin_manager.get_plugin_source_name(plugin) for plugin in plugins_used)
        if self.config['show_usage']:
            if not self.config.get('use_responses_api', False):
                answer += "\n\n---\n" \
                          f"💰 {str(response.usage.total_tokens)} {localized_text('stats_tokens', bot_language)}" \
                          f" ({str(response.usage.prompt_tokens)} {localized_text('prompt', bot_language)}," \
                          f" {str(response.usage.completion_tokens)} {localized_text('completion', bot_language)})"
            else:
                usage_total = getattr(response, 'usage', None).total_tokens if getattr(response, 'usage', None) else self.__count_tokens(self.conversations[chat_id])
                answer += "\n\n---\n" \
                          f"💰 {str(usage_total)} {localized_text('stats_tokens', bot_language)}"
            # if show_plugins_used:
            #     answer += f"\n🔌 {', '.join(plugin_names)}"
        # elif show_plugins_used:
        #     answer += f"\n\n---\n🔌 {', '.join(plugin_names)}"

        return answer, response.usage.total_tokens

    async def interpret_image_stream(self, chat_id, fileobj, prompt=None):
        """
        Interprets a given PNG image file using the Vision model.
        """
        image = encode_image(fileobj)
        prompt = self.config['vision_prompt'] if prompt is None else prompt

        content = [{'type':'text', 'text':prompt}, {'type':'image_url', \
                    'image_url': {'url':image, 'detail':self.config['vision_detail'] } }]

        response = await self.__common_get_chat_response_vision(chat_id, content, stream=True)

        

        # if self.config['enable_functions']:
        #     response, plugins_used = await self.__handle_function_call(chat_id, response, stream=True)
        #     if is_direct_result(response):
        #         yield response, '0'
        #         return

        if not self.config.get('use_responses_api', False):
            answer = ''
            async for chunk in response:
                if len(chunk.choices) == 0:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    answer += delta.content
                    yield answer, 'not_finished'
            answer = answer.strip()
            self.__add_to_history(chat_id, role="assistant", content=answer)
            tokens_used = str(self.__count_tokens(self.conversations[chat_id]))
            if self.config['show_usage']:
                answer += f"\n\n---\n💰 {tokens_used} {localized_text('stats_tokens', self.config['bot_language'])}"
            yield answer, tokens_used
        else:
            # Streaming via Responses for vision
            model_to_use = self.config['vision_model']
            # Build input from last prepared content
            input_messages = []
            for m in self.conversations[chat_id]:
                c = m['content']
                if isinstance(c, str):
                    input_messages.append({'role': m['role'], 'content': [{'type': 'input_text', 'text': c}]})
                else:
                    input_messages.append({'role': m['role'], 'content': c})
            # Replace last user message with structured content
            input_messages = input_messages[:-1] + [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': prompt},
                {'type': 'input_image', 'image_url': {'url': image, 'detail': self.config['vision_detail']}}
            ]}]

            answer = ''
            try:
                async with self.client.responses.stream(
                    model=model_to_use,
                    input=input_messages,
                    temperature=self.config['temperature'],
                    max_output_tokens=self.config['vision_max_tokens']
                ) as stream:
                    async for event in stream:
                        if getattr(event, 'type', None) == 'response.output_text.delta':
                            delta = getattr(event, 'delta', '')
                            if delta:
                                answer += delta
                                yield answer, 'not_finished'
                    final = await stream.get_final_response()
                    answer = answer.strip()
                    self.__add_to_history(chat_id, role="assistant", content=answer)
                    tokens_used = getattr(final, 'usage', None).total_tokens if getattr(final, 'usage', None) else self.__count_tokens(self.conversations[chat_id])
                    if self.config['show_usage']:
                        answer += f"\n\n---\n💰 {tokens_used} {localized_text('stats_tokens', self.config['bot_language'])}"
                    yield answer, str(tokens_used)
            except Exception as e:
                logging.warning(f'Responses vision stream failed, falling back: {str(e)}')
                interpretation, total_tokens = await self.interpret_image(chat_id, fileobj, prompt)
                yield interpretation, total_tokens

        #show_plugins_used = len(plugins_used) > 0 and self.config['show_plugins_used']
        #plugin_names = tuple(self.plugin_manager.get_plugin_source_name(plugin) for plugin in plugins_used)
        if self.config['show_usage']:
            answer += f"\n\n---\n💰 {tokens_used} {localized_text('stats_tokens', self.config['bot_language'])}"
        #     if show_plugins_used:
        #         answer += f"\n🔌 {', '.join(plugin_names)}"
        # elif show_plugins_used:
        #     answer += f"\n\n---\n🔌 {', '.join(plugin_names)}"

        yield answer, tokens_used

    def reset_chat_history(self, chat_id, content=''):
        """
        Resets the conversation history.
        """
        if content == '':
            content = self.config['assistant_prompt']
        self.conversations[chat_id] = [{"role": "assistant" if self.config['model'] in O_MODELS else "system", "content": content}]
        self.conversations_vision[chat_id] = False

    def __max_age_reached(self, chat_id) -> bool:
        """
        Checks if the maximum conversation age has been reached.
        :param chat_id: The chat ID
        :return: A boolean indicating whether the maximum conversation age has been reached
        """
        if chat_id not in self.last_updated:
            return False
        last_updated = self.last_updated[chat_id]
        now = datetime.datetime.now()
        max_age_minutes = self.config['max_conversation_age_minutes']
        return last_updated < now - datetime.timedelta(minutes=max_age_minutes)

    def __add_function_call_to_history(self, chat_id, function_name, content):
        """
        Adds a function call to the conversation history
        """
        self.conversations[chat_id].append({"role": "function", "name": function_name, "content": content})

    def __add_to_history(self, chat_id, role, content):
        """
        Adds a message to the conversation history.
        :param chat_id: The chat ID
        :param role: The role of the message sender
        :param content: The message content
        """
        self.conversations[chat_id].append({"role": role, "content": content})

    async def __summarise(self, conversation) -> str:
        """
        Summarises the conversation history.
        :param conversation: The conversation history
        :return: The summary
        """
        messages = [
            {"role": "assistant", "content": "Summarize this conversation in 700 characters or less"},
            {"role": "user", "content": str(conversation)}
        ]
        if not self.config.get('use_responses_api', False):
            response = await self.client.chat.completions.create(
                model=self.config['model'],
                messages=messages,
                temperature=1 if self.config['model'] in O_MODELS else 0.4
            )
            return response.choices[0].message.content
        else:
            resp = await self.client.responses.create(
                model=self.config['model'],
                input=[{'role':'assistant','content':[{'type':'input_text','text':'Summarize this conversation in 700 characters or less'}]},
                       {'role':'user','content':[{'type':'input_text','text': str(conversation)}]}],
                temperature=1 if self.config['model'] in O_MODELS else 0.4,
                max_output_tokens=500
            )
            return resp.output_text

    def __max_model_tokens(self):
        base = 4096
        if self.config['model'] in GPT_3_MODELS:
            return base
        if self.config['model'] in GPT_3_16K_MODELS:
            return base * 4
        if self.config['model'] in GPT_4_MODELS:
            return base * 2
        if self.config['model'] in GPT_4_32K_MODELS:
            return base * 8
        if self.config['model'] in GPT_4_VISION_MODELS:
            return base * 31
        if self.config['model'] in GPT_4_128K_MODELS:
            return base * 31
        if self.config['model'] in GPT_4O_MODELS:
            return base * 31
        if self.config['model'] in GPT_4_1_MODELS:
            return base * 64
        elif self.config['model'] in O_MODELS:
            # https://platform.openai.com/docs/models#o1
            if self.config['model'] == "o1":
                return 100_000
            elif self.config['model'] == "o1-preview":
                return 32_768
            else:
                return 65_536
        raise NotImplementedError(
            f"Max tokens for model {self.config['model']} is not implemented yet."
        )

    # https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
    def __count_tokens(self, messages) -> int:
        """
        Counts the number of tokens required to send the given messages.
        :param messages: the messages to send
        :return: the number of tokens required
        """
        model = self.config['model']
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")

        if model in GPT_ALL_MODELS:
            tokens_per_message = 3
            tokens_per_name = 1
        else:
            raise NotImplementedError(f"""num_tokens_from_messages() is not implemented for model {model}.""")
        num_tokens = 0
        for message in messages:
            num_tokens += tokens_per_message
            for key, value in message.items():
                if key == 'content':
                    if isinstance(value, str):
                        num_tokens += len(encoding.encode(value))
                    else:
                        for message1 in value:
                            if message1['type'] == 'image_url':
                                image = decode_image(message1['image_url']['url'])
                                num_tokens += self.__count_tokens_vision(image)
                            else:
                                num_tokens += len(encoding.encode(message1['text']))
                else:
                    num_tokens += len(encoding.encode(value))
                    if key == "name":
                        num_tokens += tokens_per_name
        num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
        return num_tokens

    # no longer needed

    def __count_tokens_vision(self, image_bytes: bytes) -> int:
        """
        Counts the number of tokens for interpreting an image.
        :param image_bytes: image to interpret
        :return: the number of tokens required
        """
        image_file = io.BytesIO(image_bytes)
        image = Image.open(image_file)
        model = self.config['vision_model']
        if model not in GPT_4_VISION_MODELS:
            raise NotImplementedError(f"""count_tokens_vision() is not implemented for model {model}.""")
        
        w, h = image.size
        if w > h: w, h = h, w
        # this computation follows https://platform.openai.com/docs/guides/vision and https://openai.com/pricing#gpt-4-turbo
        base_tokens = 85
        detail = self.config['vision_detail']
        if detail == 'low':
            return base_tokens
        elif detail == 'high' or detail == 'auto': # assuming worst cost for auto
            f = max(w / 768, h / 2048)
            if f > 1:
                w, h = int(w / f), int(h / f)
            tw, th = (w + 511) // 512, (h + 511) // 512
            tiles = tw * th
            num_tokens = base_tokens + tiles * 170
            return num_tokens
        else:
            raise NotImplementedError(f"""unknown parameter detail={detail} for model {model}.""")
