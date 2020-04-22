"""
Copyright (c) Facebook, Inc. and its affiliates.
"""
import json
import logging
import os
import re
import spacy
from typing import Tuple, Dict, Optional

import sentry_sdk

import preprocess
from base_agent.dialogue_manager import DialogueManager
from base_agent.dialogue_objects import (
    BotCapabilities,
    BotGreet,
    DialogueObject,
    Say,
    coref_resolve,
    process_spans,
)


###TODO wrap these, clean up
# For QA  model


from util import hash_user

sp = spacy.load("en_core_web_sm")


class NSPDialogueManager(DialogueManager):
    def __init__(self, agent, dialogue_object_classes, opts, no_ground_truth_actions=False):
        super(NSPDialogueManager, self).__init__(agent, None)
        # "dialogue_object_classes" should be a dict with keys
        # interpeter, get_memory, and put_memory;
        # the values are the corresponding classes
        self.dialogue_objects = dialogue_object_classes
        self.QA_model = None
        # the following are still scripted and are handled directly from here
        self.botCapabilityQuery = [
            "what can you do",
            "what else can you do",
            "what do you know",
            "tell me what you can do",
            "what things can you do",
            "what are your capabilities",
            "show me what you can do",
            "what are you capable of",
            "help me",
            "help",
            "do something",
        ]
        self.botGreetings = ["hi", "hello", "hey"]
        logging.info("using QA_model_path={}".format(opts.QA_nsp_model_path))
        logging.info("using model_dir={}".format(opts.nsp_model_dir))

        # Instantiate the QA model
        if opts.QA_nsp_model_path:
            from ttad.ttad_model.ttad_model_wrapper import ActionDictBuilder

            self.QA_model = ActionDictBuilder(
                opts.QA_nsp_model_path,
                embeddings_path=opts.nsp_embeddings_path,
                action_tree_path=opts.nsp_grammar_path,
            )

        # Instantiate the main model
        if opts.nsp_data_dir is not None:
            from ttad.ttad_transformer_model.query_model import TTADBertModel as Model

            self.model = Model(model_dir=opts.nsp_model_dir, data_dir=opts.nsp_data_dir)
        self.debug_mode = False

        # ground_truth_data is the ground truth action dict from templated
        # generations and will be queried first if checked in.
        self.ground_truth_actions = {}
        if not no_ground_truth_actions:
            if os.path.isfile(opts.ground_truth_file_path):
                with open(opts.ground_truth_file_path) as f:
                    for line in f.readlines():
                        text, logical_form = line.strip().split("\t")
                        self.ground_truth_actions[text] = json.loads(logical_form)

        self.dialogue_object_parameters = {
            "agent": self.agent,
            "memory": self.agent.memory,
            "dialogue_stack": self.dialogue_stack,
        }

    def maybe_get_dialogue_obj(self, chat: Tuple[str, str]) -> Optional[DialogueObject]:
        """Process a chat and maybe modify the dialogue stack"""

        if len(self.dialogue_stack) > 0 and self.dialogue_stack[-1].awaiting_response:
            return None

        # chat is a single line command
        speaker, chatstr = chat
        preprocessed_chatstrs = preprocess.preprocess_chat(chatstr)

        # Push appropriate DialogueObjects to stack if incomign chat
        # is one of the scripted ones
        if any([chat in self.botCapabilityQuery for chat in preprocessed_chatstrs]):
            return BotCapabilities(**self.dialogue_object_parameters)
        if any([chat in self.botGreetings for chat in preprocessed_chatstrs]):
            return BotGreet(**self.dialogue_object_parameters)

        # don't use preprocess for models, done in the model code
        logical_form = self.get_logical_form(s=chatstr, model=self.model)
        return self.handle_logical_form(speaker, logical_form, preprocessed_chatstrs[0])

    def handle_logical_form(self, speaker: str, d: Dict, chatstr: str) -> Optional[DialogueObject]:
        """Return the appropriate DialogueObject to handle an action dict "d"

        "d" should have spans resolved by corefs not yet resolved to a specific
        MemoryObject
        """
        coref_resolve(self.agent.memory, d, chatstr)
        logging.info('logical form post-coref "{}" -> {}'.format(hash_user(speaker), d))

        if d["dialogue_type"] == "NOOP":
            return Say("I don't know how to answer that.", **self.dialogue_object_parameters)
        elif d["dialogue_type"] == "HUMAN_GIVE_COMMAND":
            return self.dialogue_objects["interpreter"](
                speaker, d, **self.dialogue_object_parameters
            )
        elif d["dialogue_type"] == "PUT_MEMORY":
            return self.dialogue_objects["put_memory"](
                speaker, d, **self.dialogue_object_parameters
            )
        elif d["dialogue_type"] == "GET_MEMORY":
            logging.info("this model out: %r" % (d))
            logging.info("querying QA model now")
            if self.QA_model:
                QA_model_d = self.get_logical_form(
                    s=chatstr, model=self.QA_model, chat_as_list=True
                )
                logging.info("QA model out: %r" % (QA_model_d))
                if (
                    QA_model_d["dialogue_type"] != "GET_MEMORY"
                ):  # this happens sometimes when new model sayas its an Answer action but previous says noop
                    return Say(
                        "I don't know how to answer that.", **self.dialogue_object_parameters
                    )
                return self.dialogue_objects["get_memory"](
                    speaker, QA_model_d, **self.dialogue_object_parameters
                )
            else:
                return self.dialogue_objects["get_memory"](
                    speaker, d, **self.dialogue_object_parameters
                )
        else:
            raise ValueError("Bad dialogue_type={}".format(d["dialogue_type"]))

    def get_logical_form(self, s: str, model, chat_as_list=False) -> Dict:
        """Query model to get the logical form"""
        if s in self.ground_truth_actions:
            d = self.ground_truth_actions[s]
            logging.info('Found gt action for "{}"'.format(s))
        else:
            if chat_as_list:
                d = model.parse([s])
            else:
                d = model.parse(chat=s)  # self.ttad_model.parse(chat=s)

        # perform lemmatization on the chat
        logging.info('chat before lemmatization "{}"'.format(s))
        lemmatized_chat = sp(s)
        chat = " ".join(str(word.lemma_) for word in lemmatized_chat)
        logging.info('chat after lemmatization "{}"'.format(chat))

        # Get the words from indices in spans
        process_spans(d, re.split(r" +", s), re.split(r" +", chat))
        logging.info('ttad pre-coref "{}" -> {}'.format(chat, d))

        # log to sentry
        sentry_sdk.capture_message(
            json.dumps({"type": "ttad_pre_coref", "in_original": s, "out": d})
        )
        sentry_sdk.capture_message(
            json.dumps({"type": "ttad_pre_coref", "in_lemmatized": chat, "out": d})
        )
        return d