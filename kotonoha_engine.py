import hashlib
import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from kotonoha_progress import LiveProgress
from kotonoha_storage import DATA_DIR
from kotonoha_storage import atomic_json as atomic_write_json

MODEL = "hf.co/mradermacher/SuperGemma-4-12b-abliterated-GGUF:IQ4_XS"

BASE_DIR = Path(__file__).resolve().parent
GLOSSARY_PATH = BASE_DIR / "glossary-runtime.json"
CACHE_PATH = DATA_DIR / "translation-cache.json"
DEFAULT_LOG_NAME = "rename-log.json"
PREFERENCES_PATH = DATA_DIR / "translation_preferences.json"
PROPER_NAME_CACHE_PATH = DATA_DIR / "proper_name_cache.json"

FORMAT_FOLDER_NAMES = {
    "WAV",
    "FLAC",
    "MP3",
    "M4A",
    "AAC",
    "OGG",
    "OPUS",
    "JPG",
    "JPEG",
    "PNG",
    "WEBP",
    "PDF",
    "TXT",
}

CHAT_TRANSPORT = ContextVar("kotonoha_chat_transport", default=None)

MAX_RETRIES = 3
PROMPT_VERSION = "kotonoha-title-translation-1"
TRANSLATION_OPTIONS = {"temperature": 0, "num_ctx": 4096, "num_predict": 4096}

# 프로그램이 생성하는 메타데이터 파일은 번역/rename 대상에서 제외한다.
EXCLUDED_FILENAMES = {
    DEFAULT_LOG_NAME.lower(),
    CACHE_PATH.name.lower(),
    GLOSSARY_PATH.name.lower(),
    PREFERENCES_PATH.name.lower(),
    PROPER_NAME_CACHE_PATH.name.lower(),
}


BASE_SYSTEM_PROMPT = """다음은 성인용 ASMR 작품의 일본어 파일 제목입니다.
한국어 오타쿠가 봤을 때 자연스럽도록 번역해주세요.
원작 제목의 일본 서브컬처 특유의 느낌과 말맛을 최대한 살려주세요.
사용자 선호와 가나/카타카나 서브컬처 표현은 음역이 더 자연스러우면 음역해도 됩니다.
고유명사는 작품용 proper-name map을 따르세요. 일반 명사와 시적인 한자 합성어는 발음이 아니라 뜻을 번역하세요.
날씨나 자연 현상에 관한 제목을 캐릭터 호명으로 읽지 마세요. 예를 들어 봄바람을 '하루카제'라는 이름으로 만들지 마세요.
ASCII 영문과 약어는 원문 표기를 유지하세요.
원문의 장식문자·강조·반복 횟수는 가능한 한 유지해주세요.
지정한 JSON 형식으로 번역 결과만 출력하세요."""


# Windows에서 사용할 수 없는 ASCII 문자는 가능한 한 분위기를 보존하도록
# 전각 문자로 치환한다. 제어문자만 '_'로 대체한다.
WINDOWS_CHAR_MAP = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        ":": "：",
        '"': "＂",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        "?": "？",
        "*": "＊",
    }
)

CONTROL_CHARS = re.compile(r"[\x00-\x1f]")

RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

# -----------------------------------------------------------------------------
# JSON / filesystem helpers
# -----------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(data) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return sha256_bytes(encoded)


# -----------------------------------------------------------------------------
# Glossary
# -----------------------------------------------------------------------------


def load_glossary():
    if not GLOSSARY_PATH.exists():
        raise FileNotFoundError(f"용어집 파일을 찾을 수 없습니다: {GLOSSARY_PATH}")

    with GLOSSARY_PATH.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


GLOSSARY = load_glossary()
GLOSSARY_HASH = sha256_bytes(GLOSSARY_PATH.read_bytes())


def load_preferences():
    # 삭제/빈 terms로 사용자 선호를 끌 수 있다. 잘못된 JSON은 조용히 무시하지 않는다.
    if not PREFERENCES_PATH.exists():
        return {"version": "1.0", "terms": {}}
    with PREFERENCES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("terms"), dict):
        raise ValueError("translation_preferences.json의 terms는 객체여야 합니다.")
    for term, translation in data["terms"].items():
        if (
            not isinstance(term, str)
            or not term
            or not isinstance(translation, str)
            or not translation.strip()
        ):
            raise ValueError("preference의 원문과 번역은 비어 있지 않은 문자열이어야 합니다.")
    return data


PREFERENCES = load_preferences()
PREFERENCES_HASH = (
    sha256_bytes(PREFERENCES_PATH.read_bytes())
    if PREFERENCES_PATH.exists()
    else sha256_json(PREFERENCES)
)


# -----------------------------------------------------------------------------
# Filename / work-context utilities
# -----------------------------------------------------------------------------


def sanitize_name(name: str, is_file_stem=False) -> str:
    # 원문의 시각적 느낌은 유지하고 Windows 금칙 ASCII만 전각 문자로 치환한다.
    name = name.translate(WINDOWS_CHAR_MAP)
    name = CONTROL_CHARS.sub("_", name)

    # ASCII 제어성 공백만 정리한다. 일본어 전각 공백(U+3000)은 보존한다.
    name = re.sub(r"[ \t\r\n\f\v]+", " ", name).strip(" ")

    trailing_dots = len(name) - len(name.rstrip("."))
    if trailing_dots:
        name = name[:-trailing_dots] + ("" if is_file_stem else "．" * trailing_dots)

    if not name:
        name = "unnamed"

    if name.split(".", 1)[0].rstrip(" ").upper() in RESERVED_NAMES:
        name = "_" + name

    return name


LEADING_PREFIX_PATTERNS = [
    # [Track01] / Track01 / 【Track01】
    re.compile(
        r"^(\s*[\[\【\(（]?\s*Track\s*\d+"
        r"\s*[\]\】\)）]?\s*[-_. 　]*)(.*)$",
        re.IGNORECASE,
    ),
    # 【03】, [03], (03), 【02-1】
    re.compile(
        r"^(\s*[\[\【\(（]\s*\d{1,3}(?:[-_.]\d{1,3})*"
        r"\s*[\]\】\)）]\s*[-_. 　]*)(.*)$"
    ),
    # 01　Title / 02-1　Title / 03_2 Title
    re.compile(
        r"^(\s*\d{1,3}(?:[-_.]\d{1,3})*"
        r"(?:[ 　]+|[-_.]+[ 　]*))(.*)$"
    ),
]


def split_leading_prefix(stem: str):
    """
    번호 형식을 재작성하지 않고 원문 그대로 떼어 둔다.
    예:
      '02-1　タイトル' -> ('02-1　', 'タイトル')
      '【03】タイトル' -> ('【03】', 'タイトル')
      '[Track01] title' -> ('[Track01] ', 'title')
    """
    for pattern in LEADING_PREFIX_PATTERNS:
        match = pattern.match(stem)
        if match:
            return match.group(1), match.group(2).strip()

    return "", stem


JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

# 문자 잔존 여부만 살핀다. 가운데점·장음 같은 장식은 일본어로 판정하지 않는다.
RESIDUAL_JAPANESE_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u3400-\u4dbf\u4e00-\u9fff]")


def has_untranslated_japanese(text: str) -> bool:
    return bool(RESIDUAL_JAPANESE_RE.search(text))


def compact_term_info(info):
    compact = {
        key: info[key] for key in ("preferred", "confidence", "preferred_status") if info.get(key)
    }
    common = [
        value
        for value in info.get("common_translations", [])
        if isinstance(value, str) and value != info.get("preferred")
    ][:2]
    if common:
        compact["common_translations"] = common
    # 긴 연구용 notes 대신 다른 실제 번역의 짧은 용법만 선택한다.
    contextual = []
    for item in info.get("contextual_translations", []):
        if not isinstance(item, dict):
            continue
        translation = item.get("translation")
        note = item.get("usage_note", "")
        if (
            isinstance(translation, str)
            and translation != info.get("preferred")
            and isinstance(note, str)
            and 0 < len(note) <= 45
        ):
            contextual.append({"translation": translation, "usage_note": note})
        if len(contextual) == 1:
            break
    if contextual:
        compact["contextual_translations"] = contextual
    return compact


def collect_relevant_glossary(texts):
    """긴 표현의 매칭 범위를 먼저 확보한다. 다른 위치의 짧은 표현은 살린다."""
    candidates = {}
    for section in ("fixed_terms", "contextual_terms"):
        for term, info in GLOSSARY.get(section, {}).items():
            if term and isinstance(info, dict) and info.get("preferred"):
                candidates[term] = compact_term_info(info)
    # 정확히 같은 term의 충돌은 preference가 우선한다.
    for term, translation in PREFERENCES.get("terms", {}).items():
        candidates[term] = {
            "preferred": translation,
            "preferred_status": "user_preference",
        }
    ordered = sorted(candidates, key=lambda term: (-len(term), term))
    selected = {}
    for text in texts:
        occupied = [False] * len(text)
        for term in ordered:
            start = 0
            while True:
                position = text.find(term, start)
                if position < 0:
                    break
                end = position + len(term)
                if not any(occupied[position:end]):
                    occupied[position:end] = [True] * len(term)
                    selected[term] = candidates[term]
                start = position + 1
    return {term: selected[term] for term in ordered if term in selected}


def make_glossary_prompt(texts):
    relevant = collect_relevant_glossary(texts)
    if not relevant:
        return ""
    lines = []
    for term, info in relevant.items():
        details = []
        if info.get("preferred_status") == "user_preference":
            details.append("사용자 선호")
        else:
            if info.get("confidence"):
                details.append(info["confidence"])
            if info.get("preferred_status"):
                details.append(info["preferred_status"])
            if info.get("common_translations"):
                details.append("다른 용례: " + "/".join(info["common_translations"]))
            for item in info.get("contextual_translations", []):
                details.append(item["translation"] + ": " + item["usage_note"])
        suffix = " (" + "; ".join(details) + ")" if details else ""
        lines.append(f"- {term} → {info['preferred']}{suffix}")
    return (
        "\n\n아래 용어집은 실제 일본어→한국어 용례와 사용자 선호 표현입니다.\n"
        "문맥에 맞는 경우 우선 참고하되, 기계적으로 치환하지 말고 자연스러운 제목으로 완성해주세요.\n"
        "용어집:\n" + "\n".join(lines)
    )


# -----------------------------------------------------------------------------
# Ollama helpers with retry
# -----------------------------------------------------------------------------

_MODEL_DEPTH = {}
_MODEL_TOUCHED = set()


def unload_model(model):
    """Unload only this translation's model; keep the Ollama server running."""
    from ollama import Client

    try:
        Client(timeout=10).generate(model=model, keep_alive=0)
        print(f"Ollama 모델 메모리 해제 완료: {model}", flush=True)
    except Exception as exc:
        print(
            f"경고: Ollama 모델 해제 요청 실패: {exc}\n수동 해제: ollama stop {model}", flush=True
        )


@contextmanager
def model_scope(model):
    # Nested scopes let name analysis and the initial batch share one load.
    _MODEL_DEPTH[model] = _MODEL_DEPTH.get(model, 0) + 1
    try:
        yield
    finally:
        _MODEL_DEPTH[model] -= 1
        if not _MODEL_DEPTH[model]:
            _MODEL_DEPTH.pop(model)
            if model in _MODEL_TOUCHED:
                _MODEL_TOUCHED.discard(model)
                unload_model(model)


def releases_model(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with model_scope(self.model):
            return function(self, *args, **kwargs)

    return wrapped


def ollama_chat_with_retry(**kwargs):
    from ollama import chat  # --help/--undo에는 Ollama 연결이 필요 없다.

    chat = CHAT_TRANSPORT.get() or chat
    last_error = None
    on_text = kwargs.pop("on_text", None)
    # A killed process cannot run finally; bound remaining residency to one minute.
    kwargs.setdefault("keep_alive", "1m")
    _MODEL_TOUCHED.add(kwargs["model"])

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if on_text is None:
                return chat(**kwargs)
            on_text("")
            content = ""
            for chunk in chat(**kwargs, stream=True):
                content += chunk.message.content or ""
                on_text(content)
            return SimpleNamespace(message=SimpleNamespace(content=content))
        except Exception as exc:
            if getattr(exc, "cancelled", False):
                raise
            last_error = exc

            if attempt >= MAX_RETRIES:
                break

            delay = 0.75 * attempt
            print(f"\n    Ollama 오류 - 재시도 {attempt}/{MAX_RETRIES - 1} ({exc})")
            time.sleep(delay)

    raise RuntimeError(
        f"Ollama 호출이 {MAX_RETRIES}회 모두 실패했습니다: {last_error}"
    ) from last_error


# -----------------------------------------------------------------------------
# Work-level AI translation
# -----------------------------------------------------------------------------


def valid_title_output(value):
    # 의미를 판단하지 않고 빈 출력·여러 줄 등 출력 형식만 검사한다.
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(char in value.strip() for char in "\r\n")
    )


BATCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def parse_batch_results(content, requested_ids):
    """ID별 형식만 검사한다. 잘못된 항목이 다른 정상 결과를 지우지 않는다."""
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("translations"), list):
        raise ValueError("translations 배열이 없습니다.")
    results = {}
    seen = set()
    duplicates = set()
    for item in data["translations"]:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if type(identifier) is not int or identifier not in requested_ids:
            continue
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
        value = item.get("text")
        if valid_title_output(value):
            results[identifier] = value.strip()
    for identifier in duplicates:
        results.pop(identifier, None)
    return results


# -----------------------------------------------------------------------------
# Work-level proper names (model analysis, editable persistent cache)
# -----------------------------------------------------------------------------

PROPER_NAME_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "proper_names": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "korean": {"type": "string"}},
                "required": ["source", "korean"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proper_names"],
    "additionalProperties": False,
}

PROPER_NAME_SYSTEM_PROMPT = """다음은 같은 음성 작품에 포함된 일본어 제목들입니다.
사람 이름, 성우명, 캐릭터명, 서클명 등 동일한 한글 표기를 유지할 고유명사만 추출하세요.
일반 ASMR 용어, 장르명, 보통명사는 제외하세요. 원문에 실제 등장하는 이름만 반환하세요.
이름은 가능한 한 ちゃん 같은 호칭을 제외한 핵심 이름 자체로 추출하세요.
각 이름의 자연스러운 한글 표기를 하나 결정하세요. 가나/카타카나는 발음으로 음역하세요.
한자 이름은 문맥으로 읽기를 판단하되 불확실한 읽기를 일반 단어 뜻으로 번역하지 마세요.
confirmed는 사람이 확정한 표기이므로 반드시 유지하세요. learned는 과거 자동 추정 참고이며 현재 문맥에서 재판단할 수 있습니다. 고유명사가 없으면 proper_names는 빈 배열입니다.
지정한 JSON 형식으로만 출력하세요."""


def load_proper_name_cache():
    # v1.0 자동 표기는 learned로만 이관한다. 사람 확정은 JSON에서 관리한다.
    try:
        if not PROPER_NAME_CACHE_PATH.exists():
            data = {"version": "1.1", "confirmed": {}, "learned": {}}
            atomic_write_json(PROPER_NAME_CACHE_PATH, data)
            return data
        with PROPER_NAME_CACHE_PATH.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("고유명사 캐시는 객체여야 합니다.")
        legacy = "names" in data and "confirmed" not in data and "learned" not in data
        sections = [data.get("names")] if legacy else [data.get("confirmed"), data.get("learned")]
        for names in sections:
            if not isinstance(names, dict) or not all(
                valid_title_output(k) and valid_title_output(v) for k, v in names.items()
            ):
                raise ValueError("각 이름 섹션은 비어 있지 않은 한 줄 문자열의 객체여야 합니다.")
        if legacy:
            data = {"version": "1.1", "confirmed": {}, "learned": data["names"]}
            atomic_write_json(PROPER_NAME_CACHE_PATH, data)
            print("  [고유명사 캐시 migration] v1.0 names → v1.1 learned")
        return data
    except Exception as exc:
        print(f"  [경고] 고유명사 캐시를 읽을 수 없어 이번 실행에서는 저장하지 않습니다: {exc}")
        return None


def parse_proper_names(content, sources):
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("proper_names"), list):
        raise ValueError("proper_names 배열이 없습니다.")
    names = {}
    conflicts = set()
    for item in data["proper_names"]:
        if not isinstance(item, dict):
            continue
        source, korean = item.get("source"), item.get("korean")
        if not valid_title_output(source) or not valid_title_output(korean):
            print("  [경고] 고유명사 분석의 잘못된 항목 형식을 제외합니다.")
            continue
        source, korean = source.strip(), korean.strip()
        if not any(source in title for title in sources) or has_untranslated_japanese(korean):
            print(
                f"  [경고] 원문에 없거나 한글 표기가 아닌 이름 후보를 제외합니다: {source} → {korean}"
            )
            continue
        if source in names and names[source] != korean:
            conflicts.add(source)
        names[source] = korean
    for source in conflicts:
        print(f"  [경고] 분석에서 서로 다른 표기가 나온 이름을 제외합니다: {source}")
        names.pop(source, None)
    return names


def resolve_work_proper_names(sources):
    cache = load_proper_name_cache()
    confirmed_all = cache["confirmed"] if cache is not None else {}
    learned_all = cache["learned"] if cache is not None else {}
    confirmed = {
        name: value
        for name, value in confirmed_all.items()
        if any(name in title for title in sources)
    }
    learned = {
        name: value
        for name, value in learned_all.items()
        if name not in confirmed and any(name in title for title in sources)
    }

    proposed = {}
    try:
        with LiveProgress("이름 표기 확인", MODEL) as progress:
            response = ollama_chat_with_retry(
                model=MODEL,
                think=False,
                messages=[
                    {"role": "system", "content": PROPER_NAME_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "확정 표기 (confirmed): "
                        + json.dumps(confirmed, ensure_ascii=False, separators=(",", ":"))
                        + "\n과거 자동 추정 (learned; 재판단 가능): "
                        + json.dumps(learned, ensure_ascii=False, separators=(",", ":"))
                        + "\n작품 전체 제목: "
                        + json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
                format=PROPER_NAME_OUTPUT_SCHEMA,
                options={**TRANSLATION_OPTIONS, "num_predict": 1024},
                on_text=progress.text,
            )
            progress.finish()
        proposed = parse_proper_names(response.message.content, sources)

    except Exception as exc:
        if getattr(exc, "cancelled", False):
            raise
        print(
            f"  [경고] 고유명사 분석 실패; confirmed/learned를 참고하여 배치 번역을 계속합니다: {exc}"
        )
    work_map = {**learned, **proposed, **confirmed}
    changes = {
        name: value
        for name, value in proposed.items()
        if name not in confirmed_all and learned_all.get(name) != value
    }
    if changes and cache is not None:
        try:
            atomic_write_json(
                PROPER_NAME_CACHE_PATH, {**cache, "learned": {**learned_all, **changes}}
            )

        except Exception as exc:
            print(f"  [경고] 고유명사 캐시 저장 실패; 이번 작품 표기는 그대로 사용합니다: {exc}")
    return work_map, confirmed


def make_proper_name_prompt(work_map):
    if not work_map:
        return ""
    return (
        "\n\n이 작품에서 사용할 고유명사 표기:\n"
        + "\n".join(f"- {name} → {korean}" for name, korean in sorted(work_map.items()))
        + "\n이 이름 표기는 다른 제안보다 우선하여 작품 전체에서 동일하게 사용하세요. 호칭은 glossary/preference를 참고하세요."
    )


def unique_target(
    source: Path,
    new_stem: str,
    reserved: set,
):
    suffix = source.suffix if source.is_file() else ""
    candidate = source.with_name(new_stem + suffix)

    counter = 2

    while (candidate.exists() and candidate != source) or (str(candidate).lower() in reserved):
        candidate = source.with_name(f"{new_stem} ({counter}){suffix}")

        counter += 1

    reserved.add(str(candidate).lower())
    return candidate


def should_skip_file(path: Path, excluded_paths=None):
    if path.name.lower() in EXCLUDED_FILENAMES:
        return True

    if excluded_paths:
        resolved = path.resolve()
        if resolved in excluded_paths:
            return True

    return False


def should_translate_folder(path: Path):
    # WAV/FLAC/MP3 같은 포맷 폴더는 AI 호출 자체를 하지 않는다.
    if path.name.upper() in FORMAT_FOLDER_NAMES:
        return False

    return bool(JAPANESE_RE.search(path.name))
