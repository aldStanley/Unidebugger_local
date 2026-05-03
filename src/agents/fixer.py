import os
import re
import logging
from retry import retry
from utils import read_yaml
from parse import *
from prompts.tokens import *
from agents.agent import Agent

_METHOD_BODY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_method_body",
        "description": "Fetch the full source of a named method from a related file.",
        "parameters": {
            "type": "object",
            "properties": {
                "method_name": {"type": "string", "description": "The method name to fetch"},
                "file_path": {"type": "string", "description": "Optional relative file path to narrow the search"},
            },
            "required": ["method_name"],
        },
    },
}


class Fixer(Agent):
    def parse_response(self, response: str):
        patches = parse_code(response)
        # Concatenate all code blocks to support multi-file diffs
        patch = "\n".join(p.strip() for p in patches if p.strip())
        if "===" in patch:
            patch = patch[:patch.find("===")].strip()
        return {"aim": patch, "exp": parse_exp(response), "ori": response}

    def _handle_tool_call(self, name: str, args: dict, info: dict) -> str:
        if name == "get_method_body":
            rag = info.get("rag")
            if rag is None:
                return "Method index not available (RAG not built for this run)."
            return rag.get_method_body(args["method_name"], args.get("file_path"))
        return f"Unknown tool: {name}"

    def _enrich_with_called_methods(self, info: dict, pre_agent_resp: dict):
        """Append full bodies of methods called on the buggy lines (post-Locator enrichment)."""
        rag = info.get("rag")
        if rag is None or "locator" not in pre_agent_resp:
            return
        buggy_lines = [
            l for l in pre_agent_resp["locator"].splitlines()
            if "// buggy line" in l or "// missing code" in l
        ]
        if not buggy_lines:
            return
        called = set()
        for line in buggy_lines:
            # Match lowercase-starting identifiers followed by '(' — method calls, not constructors
            called.update(re.findall(r'\b([a-z][a-zA-Z0-9_]+)\s*\(', line))
        bodies = []
        for name in sorted(called):
            body = rag.get_method_body(name)
            if "not found" not in body and "not available" not in body:
                bodies.append(f"// {name}()\n{body}")
        if not bodies:
            return
        addition = "\n\nCalled methods on the buggy line(s):\n" + "\n---\n".join(bodies)
        if calculate_token(self.core_msg + addition) <= token_limit[self.model_name]["overall"]:
            self.core_msg += addition

    def __generate_core_msg(self, info, pre_agent_resp):
        if "locator" in pre_agent_resp:
            logging.info("Fix code with marks of buggy lines")
            self.core_msg = "The following code contains a bug with suspious lines labeled:\n" + pre_agent_resp["locator"]
        else:
            self.core_msg = "The following code contains a bug:\n" + info["buggy_code"]

        self._shared_msg(info, pre_agent_resp)
        if "coverage_report" in info and calculate_token(self.core_msg + info["coverage_report"]) <= token_limit[self.model_name]["overall"]:
            self.core_msg = "Code coverage for failed testcases:\n" + info["coverage_report"] + "\n" + self.core_msg

        self._enrich_with_called_methods(info, pre_agent_resp)
        logging.info(f"Current core message tokens: {calculate_token(self.core_msg)}")

    @retry((NoCodeError), tries=3, delay=5)
    def run(self, info: dict, pre_agent_resp: dict={}, *args):
        logging.info("## Running Fixer...")
        self.label_key = "labeled" if "locator" in pre_agent_resp else "unlabeled"
        self.prompts_dict = read_yaml(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../prompts/fixer.yaml"))
        if self.core_msg is None:
            self.__generate_core_msg(info, pre_agent_resp)

        messages = [
            {"role": "system", "content": self.prompts_dict["sys"][self.label_key]},
            {"role": "user", "content": self.core_msg + "\n" + self.prompts_dict["end"]},
        ]
        response = self._tool_loop(
            messages,
            [_METHOD_BODY_TOOL],
            lambda n, a: self._handle_tool_call(n, a, info),
        )
        return self.parse_response(response)

    def refine(self, assist_resp, test_res, *args):
        refine_prompt = read_yaml(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../prompts/refine.yaml"))
        return self.parse_response(self.send_message([
                    {"role": "system", "content": self.prompts_dict["sys"][self.label_key]},
                    {"role": "user", "content": self.core_msg + "\n" + self.prompts_dict["end"]},
                    {"role": "assistant", "content": assist_resp},
                    {"role": "user", "content": "\nYour generated patch fails because:\n" + test_res + refine_prompt["fixer"]}
                ]
            )
        )
