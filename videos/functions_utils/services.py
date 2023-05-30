from __future__ import annotations
import subprocess as sp
from typing import Tuple, List
import time

COMMAND_YT_DLP = [
    "yt-dlp",
    "-S",
    "res,ext:mp4:m4a",
    "--recode",
    "mp4",
    # "",
    # "-P ../../"
]


class SpCommand:
    def __init__(self, command_list: List[str], tmp_file: str) -> None:
        self.command_list = command_list
        self.tmp_file = tmp_file
        self.file_lines = self.call_command()

    def call_command(
        self,
    ) -> List[str]:
        file = open(self.tmp_file, "w")
        sp.call(self.command_list, stdout=file)
        file = open(self.tmp_file, "r")
        lines = file.readlines()
        file.close
        return lines


