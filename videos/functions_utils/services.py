from __future__ import annotations
import subprocess as sp
from typing import Tuple, List
from loguru import logger 
import time


COMMAND_YT_DLP = [
    "yt-dlp",
    "-S",
    "res,ext:mp4:m4a",
    "--recode",
    "mp4",
    "--no-cache-dir",     # disables cache
    # "",
    # "-P ../../"
]

class SpCommand:
    def __init__(self, command_list: List[str], tmp_file: str) -> None:
        self.command_list = command_list
        self.tmp_file = tmp_file
        self.file_lines = None

    def loggging_lines(self, lines):
        for line in lines:
            logger.info(line)

    def call_command(
        self,
    ) -> List[str]:
        #print(f"list of commands {self.command_list}")
        file = open(self.tmp_file, "w")
        sp.call(self.command_list, stdout=file)
        file = open(self.tmp_file, "r")
        lines = file.readlines()
        self.loggging_lines(lines)
        file.close()
        return lines


