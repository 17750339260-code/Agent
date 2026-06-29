"""
它主要用于批量评测或批量调用大模型接口，将 Excel 中的问题逐一提交到 AI 服务，
并收集答案保存到 CSV 中。

"""
from __future__ import annotations
import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request
# DEFAULT_APP_KEY = "1001300035"
# DEFAULT_SECRET_KEY = "68bbe87e123b40089c4196a30b435bbc"
# DEFAULT_URL = "https://10.10.65.213:18300/ai-inference-gateway/predict"
# DEFAULT_COMPONENT_CODE = "04101188"
# DEFAULT_MODEL = "Qwen3-235B-A22B-w8a8"

DEFAULT_APP_KEY = "1001300033"
DEFAULT_SECRET_KEY = "24e74daf74124b0b96c9cb113162a976"
DEFAULT_URL = "https://192.168.0.213:18300/ai-inference-gateway/predict"
DEFAULT_COMPONENT_CODE = "04100567"
DEFAULT_MODEL = "Qwen3-235B-A22B-w8a8"

SYSTEM_COLUMN = "名称(选填)"
USER_COLUMN = "原始问题(必填)"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
END_OF_CHAIN = 0xFFFFFFFE
FREE_SECTOR = 0xFFFFFFFF
FAT_SECTOR = 0xFFFFFFFD
DIFAT_SECTOR = 0xFFFFFFFC
MAXREGSECT = 0xFFFFFFFA
@dataclass(frozen=True)
class BatchConfig:
    input_file: Path
    output_file: Path
    url: str
    app_key: str
    secret_key: str
    component_code: str
    model: str
    max_tokens: int
    timeout: int
    verify_ssl: bool
    stream: bool
    sleep_seconds: float
    retries: int
    retry_sleep: float
    limit: int | None
    dry_run: bool
def generate_auth_headers(app_key: str, secret_key: str) -> dict[str, str]:
    curl_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
    date_str = f"x-date: {curl_date}"
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            date_str.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    authorization = (
        f'hmac username="{app_key}", '
        f'algorithm="hmac-sha256", '
        f'headers="x-date", '
        f'signature="{signature}"'
    )
    return {
        "x-date": curl_date,
        "authorization": authorization,
        "Content-Type": "application/json",
    }
def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
class OleCompoundFile:
    """Minimal OLE Compound File reader for old binary .xls files."""
    def __init__(self, data: bytes) -> None:
        if not data.startswith(OLE_MAGIC):
            raise ValueError("Not an OLE compound document")
        self.data = data
        self.sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        self.num_fat_sectors = struct.unpack_from("<I", data, 44)[0]
        self.first_dir_sector = struct.unpack_from("<I", data, 48)[0]
        self.mini_stream_cutoff = struct.unpack_from("<I", data, 56)[0]
        self.first_minifat_sector = struct.unpack_from("<I", data, 60)[0]
        self.num_minifat_sectors = struct.unpack_from("<I", data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", data, 68)[0]
        self.num_difat_sectors = struct.unpack_from("<I", data, 72)[0]
        self.fat = self._build_fat()
        self.entries = self._read_directory()
        self.root_entry = next((entry for entry in self.entries if entry["type"] == 5), None)
        self.root_stream = (
            self._read_regular_chain(self.root_entry["start"], self.root_entry["size"])
            if self.root_entry
            else b""
        )
        self.minifat = self._read_minifat()
    def _sector(self, sector_id: int) -> bytes:
        start = (sector_id + 1) * self.sector_size
        end = start + self.sector_size
        if sector_id < 0 or end > len(self.data):
            raise ValueError(f"Invalid OLE sector id: {sector_id}")
        return self.data[start:end]

    def _build_fat(self) -> list[int]:
        difat = [
            sector
            for sector in struct.unpack_from("<109I", self.data, 76)
            if sector not in (FREE_SECTOR, END_OF_CHAIN)
        ]
        next_difat = self.first_difat_sector
        for _ in range(self.num_difat_sectors):
            if next_difat in (FREE_SECTOR, END_OF_CHAIN):
                break
            sector_data = self._sector(next_difat)
            entries_per_sector = self.sector_size // 4 - 1
            values = struct.unpack_from(f"<{entries_per_sector}I", sector_data, 0)
            difat.extend(
                sector for sector in values if sector not in (FREE_SECTOR, END_OF_CHAIN)
            )
            next_difat = struct.unpack_from("<I", sector_data, self.sector_size - 4)[0]
        fat: list[int] = []
        for fat_sector in difat[: self.num_fat_sectors]:
            sector_data = self._sector(fat_sector)
            fat.extend(struct.unpack(f"<{self.sector_size // 4}I", sector_data))
        return fat
    def _read_regular_chain(self, start_sector: int, size: int | None = None) -> bytes:
        if start_sector in (FREE_SECTOR, END_OF_CHAIN):
            return b""
        chunks: list[bytes] = []
        sector = start_sector
        seen: set[int] = set()
        while sector < MAXREGSECT and sector not in seen:
            seen.add(sector)
            chunks.append(self._sector(sector))
            if sector >= len(self.fat):
                break
            sector = self.fat[sector]

        payload = b"".join(chunks)
        return payload[:size] if size is not None else payload
    def _read_directory(self) -> list[dict[str, Any]]:
        directory = self._read_regular_chain(self.first_dir_sector)
        entries: list[dict[str, Any]] = []
        for offset in range(0, len(directory), 128):
            entry = directory[offset : offset + 128]
            if len(entry) < 128:
                continue
            name_len = struct.unpack_from("<H", entry, 64)[0]
            if name_len < 2:
                continue
            raw_name = entry[: name_len - 2]
            name = raw_name.decode("utf-16le", errors="replace")
            entries.append(
                {
                    "name": name,
                    "type": entry[66],
                    "start": struct.unpack_from("<I", entry, 116)[0],
                    "size": struct.unpack_from("<Q", entry, 120)[0],
                }
            )
        return entries
    def _read_minifat(self) -> list[int]:
        if self.num_minifat_sectors == 0 or self.first_minifat_sector in (
            FREE_SECTOR,
            END_OF_CHAIN,
        ):
            return []
        data = self._read_regular_chain(
            self.first_minifat_sector, self.num_minifat_sectors * self.sector_size
        )
        return list(struct.unpack(f"<{len(data) // 4}I", data))
    def _read_mini_chain(self, start_sector: int, size: int) -> bytes:
        chunks: list[bytes] = []
        sector = start_sector
        seen: set[int] = set()
        while sector < MAXREGSECT and sector not in seen:
            seen.add(sector)
            start = sector * self.mini_sector_size
            end = start + self.mini_sector_size
            chunks.append(self.root_stream[start:end])
            if sector >= len(self.minifat):
                break
            sector = self.minifat[sector]
        return b"".join(chunks)[:size]
    def open_stream(self, *names: str) -> bytes:
        wanted = {name.lower() for name in names}
        for entry in self.entries:
            if entry["type"] != 2 or entry["name"].lower() not in wanted:
                continue
            if entry["size"] < self.mini_stream_cutoff:
                return self._read_mini_chain(entry["start"], entry["size"])
            return self._read_regular_chain(entry["start"], entry["size"])
        raise ValueError(f"OLE stream not found: {', '.join(names)}")
def iter_biff_records(data: bytes, start: int = 0) -> Iterable[tuple[int, bytes, int]]:
    offset = start
    while offset + 4 <= len(data):
        record_id, record_size = struct.unpack_from("<HH", data, offset)
        payload_start = offset + 4
        payload_end = payload_start + record_size
        if payload_end > len(data):
            break
        yield record_id, data[payload_start:payload_end], offset
        offset = payload_end
class BiffStringReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.chunk_index = 0
        self.offset = 0
    def read(self, size: int) -> bytes:
        output = bytearray()
        while size > 0:
            if self.chunk_index >= len(self.chunks):
                raise ValueError("Unexpected end of BIFF string data")
            chunk = self.chunks[self.chunk_index]
            if self.offset >= len(chunk):
                self.chunk_index += 1
                self.offset = 0
                continue
            take = min(size, len(chunk) - self.offset)
            output.extend(chunk[self.offset : self.offset + take])
            self.offset += take
            size -= take
        return bytes(output)
    def read_u8(self) -> int:
        return self.read(1)[0]

    def read_u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]
    def read_chars(self, char_count: int, options: int) -> str:
        pieces: list[str] = []
        is_16bit = bool(options & 0x01)
        remaining = char_count
        while remaining > 0:
            if self.chunk_index >= len(self.chunks):
                raise ValueError("Unexpected end of BIFF character data")
            chunk = self.chunks[self.chunk_index]
            if self.offset >= len(chunk):
                self.chunk_index += 1
                self.offset = 0
                if self.chunk_index >= len(self.chunks):
                    break
                is_16bit = bool(self.read_u8() & 0x01)
                continue
            bytes_per_char = 2 if is_16bit else 1
            available_chars = (len(chunk) - self.offset) // bytes_per_char
            take_chars = min(remaining, available_chars)
            if take_chars <= 0:
                self.chunk_index += 1
                self.offset = 0
                if self.chunk_index < len(self.chunks):
                    is_16bit = bool(self.read_u8() & 0x01)
                continue
            take_bytes = take_chars * bytes_per_char
            raw = chunk[self.offset : self.offset + take_bytes]
            self.offset += take_bytes
            remaining -= take_chars
            encoding = "utf-16le" if is_16bit else "latin1"
            pieces.append(raw.decode(encoding, errors="replace"))
        return "".join(pieces)
def read_xl_unicode_string(reader: BiffStringReader) -> str:
    char_count = reader.read_u16()
    options = reader.read_u8()
    has_ext = bool(options & 0x04)
    has_rich = bool(options & 0x08)
    rich_size = reader.read_u16() if has_rich else 0
    ext_size = reader.read_u32() if has_ext else 0
    text = reader.read_chars(char_count, options)
    if rich_size:
        reader.read(rich_size * 4)
    if ext_size:
        reader.read(ext_size)
    return text
def decode_rk(raw: int) -> float:
    value = raw >> 2
    if raw & 0x02:
        if value & 0x20000000:
            value -= 0x40000000
        result = float(value)
    else:
        result = struct.unpack("<d", struct.pack("<II", raw & 0xFFFFFFFC, 0))[0]
    if raw & 0x01:
        result /= 100
    return result
def format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)
def parse_sst(chunks: list[bytes]) -> list[str]:
    reader = BiffStringReader(chunks)
    reader.read_u32()  # total string count
    unique_count = reader.read_u32()
    strings: list[str] = []
    for _ in range(unique_count):
        strings.append(read_xl_unicode_string(reader))
    return strings
def decode_sheet_name(data: bytes) -> str:
    if len(data) < 8:
        return ""
    char_count = data[6]
    options = data[7]
    raw = data[8:]
    if options & 0x01:
        return raw[: char_count * 2].decode("utf-16le", errors="replace")
    return raw[:char_count].decode("latin1", errors="replace")
def parse_cell_label(data: bytes) -> tuple[int, int, str]:
    row, col = struct.unpack_from("<HH", data, 0)
    reader = BiffStringReader([data[6:]])
    return row, col, read_xl_unicode_string(reader)
def parse_worksheet_rows(workbook: bytes, sheet_offset: int, sst: list[str]) -> list[dict[int, str]]:
    cells: dict[tuple[int, int], str] = {}
    max_row = -1
    for record_id, payload, _ in iter_biff_records(workbook, sheet_offset):
        if record_id == 0x000A:  # EOF
            break

        if record_id == 0x00FD and len(payload) >= 10:  # LABELSST
            row, col, _xf, sst_index = struct.unpack_from("<HHHI", payload, 0)
            if sst_index < len(sst):
                cells[(row, col)] = normalize_cell(sst[sst_index])
                max_row = max(max_row, row)
        elif record_id in (0x0204, 0x00D6) and len(payload) >= 8:  # LABEL/RSTRING
            row, col, text = parse_cell_label(payload)
            cells[(row, col)] = normalize_cell(text)
            max_row = max(max_row, row)
        elif record_id == 0x0203 and len(payload) >= 14:  # NUMBER
            row, col = struct.unpack_from("<HH", payload, 0)
            value = struct.unpack_from("<d", payload, 6)[0]
            cells[(row, col)] = format_number(value)
            max_row = max(max_row, row)
        elif record_id == 0x027E and len(payload) >= 10:  # RK
            row, col = struct.unpack_from("<HH", payload, 0)
            raw = struct.unpack_from("<I", payload, 6)[0]
            cells[(row, col)] = format_number(decode_rk(raw))
            max_row = max(max_row, row)
        elif record_id == 0x00BD and len(payload) >= 10:  # MULRK
            row, first_col = struct.unpack_from("<HH", payload, 0)
            last_col = struct.unpack_from("<H", payload, len(payload) - 2)[0]
            offset = 4
            for col in range(first_col, last_col + 1):
                if offset + 6 > len(payload) - 2:
                    break
                raw = struct.unpack_from("<I", payload, offset + 2)[0]
                cells[(row, col)] = format_number(decode_rk(raw))
                max_row = max(max_row, row)
                offset += 6
        elif record_id == 0x0205 and len(payload) >= 8:  # BOOLERR
            row, col = struct.unpack_from("<HH", payload, 0)
            value = payload[6]
            is_error = payload[7]
            cells[(row, col)] = "" if is_error else ("TRUE" if value else "FALSE")
            max_row = max(max_row, row)

    rows: list[dict[int, str]] = []
    for row_index in range(max_row + 1):
        row_values = {
            col: value
            for (row, col), value in cells.items()
            if row == row_index and value != ""
        }
        rows.append(row_values)
    return rows
def read_xls_rows(input_file: Path) -> list[dict[str, Any]]:
    ole = OleCompoundFile(input_file.read_bytes())
    workbook = ole.open_stream("Workbook", "Book")

    records = list(iter_biff_records(workbook))
    sst: list[str] = []
    sheets: list[tuple[str, int]] = []
    index = 0
    while index < len(records):
        record_id, payload, _offset = records[index]
        if record_id == 0x00FC:  # SST
            chunks = [payload]
            index += 1
            while index < len(records) and records[index][0] == 0x003C:
                chunks.append(records[index][1])
                index += 1
            sst = parse_sst(chunks)
            continue
        if record_id == 0x0085 and len(payload) >= 8:  # BOUNDSHEET
            sheet_offset = struct.unpack_from("<I", payload, 0)[0]
            sheets.append((decode_sheet_name(payload), sheet_offset))
        index += 1
    if not sheets:
        raise SystemExit(f"No worksheet found in Excel file: {input_file}")
    raw_rows = parse_worksheet_rows(workbook, sheets[0][1], sst)
    if not isinstance(raw_rows, list):
        raise SystemExit(
            "Internal XLS parser error: parse_worksheet_rows() returned "
            f"{type(raw_rows).__name__}, expected list. "
            "Please check that the production script was copied completely and "
            "that parse_worksheet_rows() ends with: return rows"
        )
    header_row_index = next((i for i, row in enumerate(raw_rows) if row), None)
    if header_row_index is None:
        return []
    header_cells = raw_rows[header_row_index]
    headers = {col: normalize_cell(value) for col, value in header_cells.items()}
    rows: list[dict[str, Any]] = []
    for row_index in range(header_row_index + 1, len(raw_rows)):
        raw_row = raw_rows[row_index]
        if not raw_row:
            continue
        row = {
            header: normalize_cell(raw_row.get(col, ""))
            for col, header in headers.items()
            if header
        }
        row["_excel_row_number"] = row_index + 1
        rows.append(row)
    return rows
class TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self.current_row = []
        elif tag.lower() in {"td", "th"} and self.current_row is not None:
            self.current_cell = []
    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.append(data)
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.current_row is not None and self.current_cell is not None:
            self.current_row.append(normalize_cell(unescape("".join(self.current_cell))))
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if any(cell for cell in self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None
def decode_text_file(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
def rows_from_table(table_rows: list[list[str]]) -> list[dict[str, Any]]:
    if not table_rows:
        return []
    headers = [normalize_cell(cell) for cell in table_rows[0]]
    rows: list[dict[str, Any]] = []
    for index, values in enumerate(table_rows[1:], start=2):
        row = {header: normalize_cell(values[i]) if i < len(values) else "" for i, header in enumerate(headers) if header}
        row["_excel_row_number"] = index
        rows.append(row)
    return rows
def read_html_rows(input_file: Path) -> list[dict[str, Any]]:
    parser = TableHTMLParser()
    parser.feed(decode_text_file(input_file.read_bytes()))
    return rows_from_table(parser.rows)
def read_csv_rows(input_file: Path) -> list[dict[str, Any]]:
    text = decode_text_file(input_file.read_bytes())
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(reader, start=2):
        clean_row = {normalize_cell(key): normalize_cell(value) for key, value in row.items() if key}
        clean_row["_excel_row_number"] = index
        rows.append(clean_row)
    return rows


def read_input_rows(input_file: Path) -> list[dict[str, Any]]:
    data = input_file.read_bytes()
    if data.startswith(OLE_MAGIC):
        return read_xls_rows(input_file)
    text_start = decode_text_file(data[:4096]).lstrip().lower()
    if text_start.startswith("<") or re.search(r"<table[\s>]", text_start):
        return read_html_rows(input_file)
    return read_csv_rows(input_file)
def load_cases(input_file: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows = read_input_rows(input_file)
    columns = list(rows[0].keys()) if rows else []
    missing_columns = [col for col in (SYSTEM_COLUMN, USER_COLUMN) if col not in columns]
    if missing_columns:
        available = ", ".join(str(col) for col in columns if not str(col).startswith("_"))
        raise SystemExit(
            f"Input file is missing required column(s): {missing_columns}. "
            f"Available columns: {available}"
        )
    cases: list[dict[str, Any]] = []
    for row in rows:
        user_prompt = normalize_cell(row.get(USER_COLUMN, ""))
        if not user_prompt:
            continue
        system_prompt = normalize_cell(row.get(SYSTEM_COLUMN, ""))
        case = {
            str(col): normalize_cell(value)
            for col, value in row.items()
            if not str(col).startswith("_")
        }
        case["_excel_row_number"] = row.get("_excel_row_number", "")
        case["_system_prompt"] = system_prompt
        case["_user_prompt"] = user_prompt
        cases.append(case)
        if limit is not None and len(cases) >= limit:
            break
    return cases
def build_payload(case: dict[str, Any], config: BatchConfig) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    system_prompt = case["_system_prompt"]
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": case["_user_prompt"]})
    return {
        "componentCode": config.component_code,
        "model": config.model,
        "messages": messages,
        "stream": config.stream,
        "max_tokens": config.max_tokens,
    }
def parse_stream_line(line: bytes | str) -> tuple[str, Any]:
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    else:
        text = str(line)
    text = text.strip()
    if not text:
        return "empty", None
    payload = text[5:].strip() if text.startswith("data:") else text
    if payload == "[DONE]":
        return "done", None
    try:
        return "json", json.loads(payload)
    except json.JSONDecodeError:
        return "raw", payload
def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    chunks: list[str] = []
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("delta", "message"):
                nested = choice.get(key)
                if isinstance(nested, dict) and isinstance(nested.get("content"), str):
                    chunks.append(nested["content"])
            if isinstance(choice.get("text"), str):
                chunks.append(choice["text"])
    for key in ("content", "text", "result", "answer", "output"):
        item = value.get(key)
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, (dict, list)):
            chunks.append(extract_text(item))
    data = value.get("data")
    if isinstance(data, (dict, list)):
        chunks.append(extract_text(data))
    return "".join(chunks)
def find_usage(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict):
            return usage
        for item in value.values():
            nested = find_usage(item)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = find_usage(item)
            if nested:
                return nested
    return {}
def get_usage_value(usage: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = usage.get(key)
        if value not in (None, ""):
            return value
    return ""
def collect_response(
    response: Any,
    requested_stream: bool,
) -> tuple[str, list[Any], dict[str, Any]]:
    raw_events: list[Any] = []
    output_chunks: list[str] = []
    usage: dict[str, Any] = {}
    content_type = response.headers.get("Content-Type", "")
    if requested_stream or "text/event-stream" in content_type:
        while True:
            line = response.readline()
            if not line:
                break
            event_type, event_value = parse_stream_line(line)
            if event_type in {"empty", "done"}:
                if event_type == "done":
                    break
                continue
            raw_events.append(event_value)
            if event_type == "json":
                output_chunks.append(extract_text(event_value))
                event_usage = find_usage(event_value)
                if event_usage:
                    usage = event_usage
            else:
                output_chunks.append(str(event_value))
    else:
        text = response.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text
        raw_events.append(body)
        if isinstance(body, (dict, list)):
            output_chunks.append(extract_text(body))
            usage = find_usage(body)
        else:
            output_chunks.append(str(body))

    return "".join(output_chunks), raw_events, usage
def post_json(payload: dict[str, Any], config: BatchConfig) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = generate_auth_headers(config.app_key, config.secret_key)
    headers["Content-Length"] = str(len(body))

    http_request = request.Request(
        config.url,
        data=body,
        headers=headers,
        method="POST",
    )
    ssl_context = None if config.verify_ssl else ssl._create_unverified_context()
    return request.urlopen(http_request, timeout=config.timeout, context=ssl_context)
def execute_one(case: dict[str, Any], index: int, total: int, config: BatchConfig) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    start_perf = time.perf_counter()
    payload = build_payload(case, config)
    result: dict[str, Any] = {
        "batch_index": index,
        "total_count": total,
        "excel_row_number": case["_excel_row_number"],
        "success": False,
        "http_status": "",
        "error": "",
        "response_text": "",
        "raw_response": "",
        "prompt_tokens": "",
        "completion_tokens": "",
        "total_tokens": "",
        "elapsed_ms": "",
        "started_at": started_at,
        "ended_at": "",
        "attempts": 0,
    }

    max_attempts = config.retries + 1
    retryable_errors = (
        TimeoutError,
        ConnectionError,
        socket.timeout,
        error.URLError,
    )

    for attempt in range(1, max_attempts + 1):
        response = None
        result["attempts"] = attempt
        try:
            try:
                response = post_json(payload, config)
            except error.HTTPError as exc:
                response = exc

            status = getattr(response, "status", getattr(response, "code", ""))
            result["http_status"] = status
            response_text, raw_events, usage = collect_response(response, config.stream)
            result["response_text"] = response_text
            result["raw_response"] = json.dumps(raw_events, ensure_ascii=False)
            result["prompt_tokens"] = get_usage_value(
                usage, ("prompt_tokens", "input_tokens", "prompt_token_count")
            )
            result["completion_tokens"] = get_usage_value(
                usage,
                (
                    "completion_tokens",
                    "output_tokens",
                    "generated_tokens",
                    "completion_token_count",
                ),
            )
            result["total_tokens"] = get_usage_value(
                usage, ("total_tokens", "total_token_count")
            )

            if isinstance(status, int) and 200 <= status < 300:
                result["success"] = True
                result["error"] = ""
                break

            reason = getattr(response, "reason", "")
            result["error"] = f"HTTP {status}: {reason}"
            break
        except retryable_errors as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                print(
                    f"[{index}/{total}] attempt {attempt}/{max_attempts} failed: "
                    f"{result['error']}; retrying in {config.retry_sleep} seconds..."
                )
                time.sleep(config.retry_sleep)
                continue
            break
        except Exception as exc:  # Keep batch execution going after a single failed row.
            result["error"] = f"{type(exc).__name__}: {exc}"
            break
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    elapsed_ms = (time.perf_counter() - start_perf) * 1000
    result["elapsed_ms"] = round(elapsed_ms, 2)
    result["ended_at"] = datetime.now().isoformat(timespec="seconds")
    return result
def write_csv_header(output_file: Path, fieldnames: list[str]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
def append_csv_row(output_file: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    with output_file.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(row)
def build_output_row(result: dict[str, Any]) -> dict[str, Any]:
    return {"response_text": result.get("response_text", "")}
def parse_args() -> BatchConfig:
    script_dir = Path(__file__).resolve().parent
    default_input = script_dir / "题库样本.xls"
    default_output = script_dir / "题库样本_results.csv"
    parser = argparse.ArgumentParser(description="Batch execute Excel prompts through AI gateway.")
    parser.add_argument("--input", default=str(default_input), help="Input .xls/CSV/HTML table file")
    parser.add_argument("--output", default=str(default_output), help="Output CSV file")
    parser.add_argument("--url", default=os.getenv("AI_GATEWAY_URL", DEFAULT_URL))
    parser.add_argument("--app-key", default=os.getenv("AI_GATEWAY_APP_KEY", DEFAULT_APP_KEY))
    parser.add_argument(
        "--secret-key", default=os.getenv("AI_GATEWAY_SECRET_KEY", DEFAULT_SECRET_KEY)
    )
    parser.add_argument("--component-code", default=DEFAULT_COMPONENT_CODE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stream", action="store_true", help="Request streaming output")
    parser.add_argument("--verify-ssl", action="store_true", help="Enable SSL certificate check")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between requests")
    parser.add_argument("--retries", type=int, default=2, help="Retry count for network errors")
    parser.add_argument("--retry-sleep", type=float, default=3.0, help="Seconds to sleep before retry")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N valid rows")
    parser.add_argument("--dry-run", action="store_true", help="Validate Excel rows without API calls")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")
    if args.retries < 0:
        parser.error("--retries must be >= 0")
    if args.retry_sleep < 0:
        parser.error("--retry-sleep must be >= 0")
    return BatchConfig(
        input_file=Path(args.input).expanduser().resolve(),
        output_file=Path(args.output).expanduser().resolve(),
        url=args.url,
        app_key=args.app_key,
        secret_key=args.secret_key,
        component_code=args.component_code,
        model=args.model,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        verify_ssl=args.verify_ssl,
        stream=args.stream,
        sleep_seconds=args.sleep,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        limit=args.limit,
        dry_run=args.dry_run,
    )
def main() -> int:
    config = parse_args()
    cases = load_cases(config.input_file, config.limit)
    if not cases:
        print("No valid rows found. The user prompt column is empty.", file=sys.stderr)
        return 1
    fieldnames = ["response_text"]
    print(f"Loaded {len(cases)} valid rows from: {config.input_file}")
    print(f"Writing results to: {config.output_file}")
    if config.dry_run:
        first_case = cases[0]
        print("Dry run only. No API requests were sent.")
        print(f"First Excel row: {first_case['_excel_row_number']}")
        print(f"First system prompt length: {len(first_case['_system_prompt'])}")
        print(f"First user prompt length: {len(first_case['_user_prompt'])}")
        return 0
    write_csv_header(config.output_file, fieldnames)
    success_count = 0
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] Excel row {case['_excel_row_number']} requesting...")
        result = execute_one(case, index, len(cases), config)
        if result["success"]:
            success_count += 1
            print(
                f"[{index}/{len(cases)}] success in {result['elapsed_ms']} ms "
                f"after {result['attempts']} attempt(s)"
            )
        else:
            print(
                f"[{index}/{len(cases)}] failed after {result['attempts']} attempt(s): "
                f"{result['error']}"
            )
        append_csv_row(config.output_file, fieldnames, build_output_row(result))
        if config.sleep_seconds > 0 and index < len(cases):
            time.sleep(config.sleep_seconds)
    print(f"Done. Success: {success_count}, Failed: {len(cases) - success_count}")
    return 0 if success_count == len(cases) else 2
if __name__ == "__main__":
    raise SystemExit(main())
