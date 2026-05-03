import os
import re
import logging
from retry import retry
from utils import read_yaml
from agents.agent import Agent, RetryError
from prompts.tokens import *
from local_rag import LocalRAG


class Helper(Agent):
    def parse_response(self, response: str):
        match = re.search(r"(.*?)===\s*(.*?)\s*===", response, re.DOTALL)
        if match:
            return {"ori": response, "aim": match.group(1).strip(), "exp": match.group(2).strip()}
        else:
            raise RetryError(response)

    def __generate_core_msg(self, info):
        self.core_msg = "The following code contains a bug:\n" + info["buggy_code"]
        self.core_msg += "\nThe code fails on this test:\n" + info["failing_test_cases"]

    @retry((RetryError), tries=3, delay=5)
    def run(self, info: dict, *args):
        logging.info("## Running Helper (local RAG)...")
        self.prompts_dict = read_yaml(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../prompts/helper.yaml"))
        if self.core_msg is None:
            self.__generate_core_msg(info)

        repo_path = info["project_meta"].get("project_src_path", info["project_meta"]["checkout_dir"])
        rag = LocalRAG(repo_path, openai_client=self.client)
        rag.build_index()
        context_text, top_entries = rag.query(info["buggy_code"], top_k=5)
        logging.info(f"## LocalRAG: indexed {len(rag.index)} symbols, retrieved {len(top_entries)} top entries")

        file_refs = "\n".join(f"{e.file_path}:{e.line}" for e in top_entries)

        user_content = (
            self.core_msg
            + "\n\nRetrieved local symbols:\n" + context_text
            + "\n\n" + self.prompts_dict["end"]
        )

        response = self.send_message(
            msg=[
                {"role": "system", "content": self.prompts_dict["sys"]},
                {"role": "user", "content": user_content},
            ]
        )

        try:
            result = self.parse_response(response)
        except RetryError:
            if file_refs:
                return {"ori": response, "aim": response, "exp": file_refs}
            raise

        if not result["exp"].strip() and file_refs:
            result["exp"] = file_refs

        return result
