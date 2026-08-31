import multiprocessing
import traceback
import atexit
from dataclasses import asdict
from pathlib import Path
import json

from hydra import initialize_config_dir, compose
from omegaconf import OmegaConf

import utils.excepthook  # noqa
import functools
import os
import platform
import socket
import subprocess
import sys
import threading
import uuid
from typing import Callable, Any, Tuple, Dict

import io
import hmac
import hashlib
import multiprocessing as mp
import queue as queue_mod
import datetime
import secrets
import time

import webview
import werkzeug.serving
from flask import Flask, render_template, request, Response, jsonify, send_file, url_for

from utils import routed_pickle
from config import InferenceConfig
from osuT5.osuT5.event import ContextType
from osuT5.osuT5.inference.server import InferenceClient
from osuT5.osuT5.utils import load_model_loaders
from inference import compile_args, get_server_address, main, should_load_separate_timing_model
from inpainting.preview import DANSER_VERSION, DanserPreviewer, PreviewError, find_danser_executable
from inpainting.preview_window import PreviewWindowController, preview_map_data
from inpainting.session import BeatmapsetSession
from inpainting.ui import compose_inpainting_config, parse_timestamp_ms, session_payload
from inpainting.workflow import generation_revision_metadata, regenerate_interval, restore_snapshot

script_dir = os.path.dirname(os.path.abspath(__file__))
template_folder = os.path.join(script_dir, 'template')
static_folder = os.path.join(script_dir, 'static')
INPAINT_OUTPUT_DIRECTORY = Path(script_dir).parent / "inpaint_output"
descriptor_dataset_paths = {
    'omdb': Path(script_dir) / 'datasets' / 'omdb_descriptors.json',
    'user_tags': Path(script_dir) / 'datasets' / 'tags_2026.json',
}

if not os.path.isdir(static_folder):
    print(f"Warning: Static folder not found at {static_folder}. Ensure it exists and contains your CSS/images.")


def format_descriptor_group_title(group_key: str) -> str:
    return ' '.join(part.capitalize() for part in group_key.replace('_', ' ').split())


def load_descriptor_set(dataset_path: Path, set_name: str) -> dict:
    if not dataset_path.is_file():
        print(f"Warning: Descriptor dataset not found at {dataset_path}.")
        return {'groups': []}

    with dataset_path.open('r', encoding='utf-8') as f:
        tag_data = json.load(f)

    groups = []
    groups_by_key = {}

    for tag in tag_data.get('tags', []):
        full_name = (tag.get('name') or '').strip()
        if not full_name:
            continue

        if '/' in full_name:
            group_key, descriptor_name = full_name.split('/', 1)
        else:
            group_key, descriptor_name = 'other', full_name

        group = groups_by_key.get(group_key)
        if group is None:
            group = {
                'key': group_key,
                'title': format_descriptor_group_title(group_key),
                'items': [],
            }
            groups_by_key[group_key] = group
            groups.append(group)

        descriptor_value = (tag.get('value') or full_name).strip()
        if not descriptor_value:
            continue

        group['items'].append({
            'value': descriptor_value,
            'label': descriptor_name,
            'title': tag.get('description') or '',
            'rulesetId': tag.get('ruleset_id'),
            'translationKey': tag.get('translation_key') or (f"tag_{tag['id']}" if set_name == 'user_tags' else descriptor_value),
        })

    return {'groups': groups}


DESCRIPTOR_SETS = {
    set_name: load_descriptor_set(dataset_path, set_name)
    for set_name, dataset_path in descriptor_dataset_paths.items()
}


# Set Flask environment to production before initializing Flask app to silence warning
# os.environ['FLASK_ENV'] = 'production' # Removed, using cli patch instead

# --- Werkzeug Warning Suppressor Patch ---
def _ansi_style_supressor(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Tuple[Any, ...], **kwargs: Dict[str, Any]) -> Any:
        # Check if the first argument is the specific warning string
        if args:
            first_arg = args[0]
            if isinstance(first_arg, str) and first_arg.startswith('WARNING: This is a development server.'):
                return ''  # Return empty string to suppress
        # Otherwise, call the original function
        return func(*args, **kwargs)

    return wrapper


# Apply the patch before Flask initialization
# noinspection PyProtectedMember
werkzeug.serving._ansi_style = _ansi_style_supressor(werkzeug.serving._ansi_style)
# --- End Patch ---

if hasattr(webview, "FileDialog"):
    OPEN_DIALOG = webview.FileDialog.OPEN
    FOLDER_DIALOG = webview.FileDialog.FOLDER
    SAVE_DIALOG = webview.FileDialog.SAVE
else:
    OPEN_DIALOG = webview.OPEN_DIALOG
    FOLDER_DIALOG = webview.FOLDER_DIALOG
    SAVE_DIALOG = webview.SAVE_DIALOG


def parse_file_dialog_result(result):
    if not result:
        return None
    return result[0] if isinstance(result, (list, tuple)) else result

app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
app.secret_key = os.urandom(24)  # Set a secret key for Flask
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
)

CSRF_HEADER_NAME = 'X-Mapperatorinator-CSRF-Token'
LOCAL_UI_CSRF_TOKEN = secrets.token_urlsafe(32)
CSRF_PROTECTED_ENDPOINTS = {
    'start_inference',
    'cancel_inference',
    'save_config',
    'validate_paths',
    'open_folder',
    'open_log_file',
    'open_inpaint_session',
    'select_inpaint_difficulty',
    'start_inpaint',
    'export_inpaint_session',
    'close_inpaint_session',
    'preview_inpaint_session',
    'configure_inpaint_preview_window',
    'get_inpaint_preview_window_state',
    'get_inpaint_preview_window_data',
    'play_inpaint_preview_window',
    'copy_inpaint_preview_boundary',
    'stop_inpaint_preview_window',
}


def _is_authorized_ui_request() -> bool:
    token = request.headers.get(CSRF_HEADER_NAME, '')
    return bool(token) and hmac.compare_digest(token, LOCAL_UI_CSRF_TOKEN)


@app.before_request
def _protect_local_ui_endpoints():
    if request.endpoint not in CSRF_PROTECTED_ENDPOINTS:
        return None

    if request.method != 'POST':
        return jsonify({
            "status": "error",
            "message": "This endpoint only accepts authenticated POST requests."
        }), 405

    if not _is_authorized_ui_request():
        return jsonify({
            "status": "error",
            "message": "Missing or invalid CSRF token. Refresh the UI and try again."
        }), 403

    return None


# --- pywebview API Class ---
class Api:
    # No __init__ needed as we get the window dynamically
    def set_window_title(self, title):
        """Updates the native pywebview window title."""
        if not webview.windows:
            print("Error: No pywebview window found.")
            return False

        try:
            webview.windows[0].set_title(title)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def save_file(self, filename):
        """Opens a save file dialog and returns the selected file path."""
        # Get the window dynamically from the global list
        if not webview.windows:
            print("Error: No pywebview window found.")
            return None
        current_window = webview.windows[0]
        result = current_window.create_file_dialog(SAVE_DIALOG, save_filename=filename)
        print(f"File dialog result: {result}")  # Debugging
        return parse_file_dialog_result(result)

    def browse_file(self, file_types=None):
        """Opens a file dialog and returns the selected file path."""
        # Get the window dynamically from the global list
        if not webview.windows:
            print("Error: No pywebview window found.")
            return None

        current_window = webview.windows[0]

        # File type filter
        try:
            if file_types and isinstance(file_types, list):
                file_types = tuple(file_types)

            result = current_window.create_file_dialog(
                OPEN_DIALOG,
                file_types=file_types
            )
        except Exception:
            result = current_window.create_file_dialog(OPEN_DIALOG)

        return parse_file_dialog_result(result)

    def browse_image(self):
        """Opens a file dialog specifically for image files and returns the selected file path."""
        # Get the window dynamically from the global list
        if not webview.windows:
            print("Error: No pywebview window found.")
            return None

        current_window = webview.windows[0]

        # Image file type filter
        image_file_types = (
            'Image Files (*.jpg;*.jpeg;*.png;*.bmp;*.gif;*.webp)',
            '*.jpg;*.jpeg;*.png;*.bmp;*.gif;*.webp',
            'JPEG Files (*.jpg;*.jpeg)',
            '*.jpg;*.jpeg',
            'PNG Files (*.png)',
            '*.png',
            'All Files (*.*)',
            '*.*'
        )

        try:
            result = current_window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=image_file_types
            )
        except Exception:
            result = current_window.create_file_dialog(OPEN_DIALOG)

        return parse_file_dialog_result(result)

    def browse_folder(self):
        """Opens a folder dialog and returns the selected folder path."""
        # Get the window dynamically from the global list
        if not webview.windows:
            print("Error: No pywebview window found.")
            return None
        current_window = webview.windows[0]
        result = current_window.create_file_dialog(FOLDER_DIALOG)
        print(f"Folder dialog result: {result}")  # Debugging
        # FOLDER_DIALOG also returns a tuple containing the path
        return parse_file_dialog_result(result)

    def open_preview_window(self):
        """Create or focus the persistent Inpaint preview controller window."""
        global preview_control_window

        if not application_base_url:
            return {"status": "error", "message": "The application URL is not ready."}

        with preview_control_window_lock:
            if preview_control_window is not None:
                try:
                    preview_control_window.restore()
                    preview_control_window.show()
                    return {"status": "focused"}
                except Exception:
                    preview_control_window = None

            try:
                window = webview.create_window(
                    "Mapperatorinpainter Preview",
                    url=f"{application_base_url}inpaint/preview-window",
                    width=1360,
                    height=1000,
                    min_size=(860, 680),
                    resizable=True,
                    background_color="#111318",
                )
                if window is None:
                    raise RuntimeError("pywebview did not create the preview window.")
                preview_control_window = window
                window.events.closed += _on_preview_control_window_closed
                return {"status": "opened"}
            except Exception as exc:
                traceback.print_exc()
                return {"status": "error", "message": str(exc)}


# --- Shared State for Inference Processes ---
# Track inference workers (multiprocessing) instead of Popen
# job_id -> {"process": mp.Process, "queue": mp.Queue, "cancelled": bool}
processes = {}
cancelled_jobs = set()
process_lock = threading.Lock()
owned_server_clients = {}
owned_server_clients_lock = threading.Lock()
inpaint_sessions = {}
inpaint_sessions_lock = threading.Lock()
inpaint_previewers = {}
inpaint_previewers_lock = threading.Lock()
preview_window_controller = PreviewWindowController()
preview_density_cache = {}
preview_density_cache_lock = threading.Lock()
preview_control_window = None
preview_control_window_lock = threading.Lock()
application_base_url = None
shutdown_lock = threading.Lock()
shutdown_started = False


def _ensure_model_server(args, *, auto_select_gamemode_model: bool, lora_path: str | None):
    socket_path = get_server_address(
        args.model_path,
        lora_path=lora_path,
        gamemode=args.gamemode,
        auto_select_gamemode_model=auto_select_gamemode_model,
    )

    with owned_server_clients_lock:
        existing_client = owned_server_clients.get(socket_path)

    if existing_client is not None:
        existing_client.ensure_server()
        return

    model_loader, tokenizer_loader = load_model_loaders(
        ckpt_path=args.model_path,
        t5_args=args.train,
        device=args.device,
        precision=args.precision,
        attn_implementation=args.attn_implementation,
        eval_mode=True,
        pickle_module=routed_pickle,
        lora_path=lora_path,
        gamemode=args.gamemode,
        auto_select_gamemode_model=auto_select_gamemode_model,
    )
    _server_owner_client = InferenceClient(
        model_loader,
        tokenizer_loader,
        max_batch_size=args.max_batch_size,
        idle_timeout=3600,
        server_thread_daemon=True,
        socket_path=socket_path,
        fast_decoder_loop=args.fast_decoder_loop,
    )

    # Start the server in a dedicated thread that outlives per-job workers.
    _server_owner_client.ensure_server()

    with owned_server_clients_lock:
        owned_server_clients.setdefault(socket_path, _server_owner_client)


def _ensure_inference_server(args):
    _ensure_model_server(
        args,
        auto_select_gamemode_model=args.auto_select_gamemode_model,
        lora_path=args.lora_path
    )

    if should_load_separate_timing_model(args):
        _ensure_model_server(args, auto_select_gamemode_model=False, lora_path=None)


def _shutdown_inference_processes():
    with process_lock:
        active_processes = list(processes.items())
        processes.clear()
        cancelled_jobs.update(job_id for job_id, _ in active_processes)

    for _, rec in active_processes:
        proc = rec.get("process")
        q = rec.get("queue")

        if proc is not None:
            try:
                if proc.is_alive():
                    if sys.platform == 'win32':
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
                    else:
                        proc.terminate()
            except Exception:
                pass

            try:
                proc.join(timeout=5)
            except Exception:
                pass

        if q is not None:
            try:
                q.cancel_join_thread()
            except Exception:
                pass
            try:
                q.close()
            except Exception:
                pass


def _shutdown_owned_model_servers():
    with owned_server_clients_lock:
        server_clients = list(owned_server_clients.values())
        owned_server_clients.clear()

    for client in server_clients:
        try:
            client.shutdown_server()
        except Exception:
            traceback.print_exc()


def _close_inpaint_previewer(session_id: str) -> None:
    with inpaint_previewers_lock:
        previewer = inpaint_previewers.pop(session_id, None)
    if previewer is not None:
        previewer.close()


def _on_preview_control_window_closed(*_args) -> None:
    global preview_control_window
    with preview_control_window_lock:
        preview_control_window = None
    snapshot = preview_window_controller.snapshot()
    if snapshot.session_id:
        _close_inpaint_previewer(snapshot.session_id)


def _destroy_preview_control_window() -> None:
    global preview_control_window
    with preview_control_window_lock:
        window = preview_control_window
        preview_control_window = None
    if window is not None:
        try:
            window.destroy()
        except Exception:
            pass


def _shutdown_inpaint_previewers() -> None:
    with inpaint_previewers_lock:
        previewers = list(inpaint_previewers.values())
        inpaint_previewers.clear()
    for previewer in previewers:
        try:
            previewer.close()
        except Exception:
            traceback.print_exc()


def _shutdown_application_resources():
    global shutdown_started

    with shutdown_lock:
        if shutdown_started:
            return
        shutdown_started = True

    _shutdown_inference_processes()
    _destroy_preview_control_window()
    _shutdown_inpaint_previewers()
    with inpaint_sessions_lock:
        sessions = list(inpaint_sessions.values())
        inpaint_sessions.clear()
    for session in sessions:
        try:
            session.cleanup()
        except Exception:
            traceback.print_exc()
    _shutdown_owned_model_servers()


# Session/model cleanup also applies when the Flask app is imported by a host,
# not only when this file launches the embedded window itself.
atexit.register(_shutdown_application_resources)


def _coerce_optional_int(v):
    if v is None or v == '':
        return None
    return int(v)


def _coerce_optional_float(v):
    if v is None or v == '':
        return None
    return float(v)


def _coerce_bool_checkbox(form, key: str) -> bool:
    return key in form


def _validate_year_for_model(model_name: str | None, year: int | None) -> None:
    if year is None:
        return

    min_year = 2007
    max_year = 2024 if model_name == 'v32' else 2023

    if year < min_year or year > max_year:
        raise ValueError(
            f"Year must be between {min_year} and {max_year} for model '{model_name or 'unknown'}'."
        )


class _QueueWriter(io.TextIOBase):
    def __init__(self, q: mp.Queue):
        self._q = q
        self._buf = ""

    def write(self, s):
        if not s:
            return 0
        self._buf += s

        # tqdm progress bars often update the same line using carriage returns.
        # Forward those updates as individual messages so the UI can parse percentage.
        while "\r" in self._buf:
            seg, self._buf = self._buf.split("\r", 1)
            if seg:
                self._q.put(seg)
            else:
                # Even an empty segment can represent a progress refresh; keep UI alive.
                self._q.put("")

        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._q.put(line)
        return len(s)

    def flush(self):
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


def _inference_worker(cfg: InferenceConfig, out_q: mp.Queue):
    """Worker entrypoint executed in a separate process (spawn-safe)."""
    import sys as _sys
    import traceback as _traceback

    try:
        # Redirect stdout/stderr to queue.
        qw = _QueueWriter(out_q)
        _sys.stdout = qw
        _sys.stderr = qw

        main(cfg)
        qw.flush()
        out_q.put({"_event": "exit", "code": 0})
    except Exception as e:
        try:
            out_q.put(str(e))
            out_q.put(_traceback.format_exc())
        except Exception:
            pass
        out_q.put({"_event": "exit", "code": 1})


def _inpaint_worker(
    cfg: InferenceConfig,
    session: BeatmapsetSession,
    out_q: mp.Queue,
    success_event: mp.Event,
):
    """Run the M2 generation transaction while reusing the UI-owned model server."""
    import sys as _sys
    import traceback as _traceback

    try:
        qw = _QueueWriter(out_q)
        _sys.stdout = qw
        _sys.stderr = qw
        regenerate_interval(session, cfg, main)
        success_event.set()
        qw.flush()
        out_q.put({"_event": "exit", "code": 0})
    except Exception as exc:
        try:
            out_q.put(str(exc))
            out_q.put(_traceback.format_exc())
        except Exception:
            pass
        out_q.put({"_event": "exit", "code": 1})


def _get_inpaint_session(session_id: str) -> BeatmapsetSession:
    with inpaint_sessions_lock:
        session = inpaint_sessions.get(session_id)
    if session is None:
        raise ValueError("Inpaint session was not found. Open the .osz again.")
    return session


def _session_has_active_job(session_id: str) -> bool:
    with process_lock:
        return any(
            record.get("inpaint_session_id") == session_id
            for record in processes.values()
        )


def _get_inpaint_previewer(session_id: str, session: BeatmapsetSession) -> DanserPreviewer:
    with inpaint_previewers_lock:
        previewer = inpaint_previewers.get(session_id)
        if previewer is not None:
            return previewer

        executable = find_danser_executable(Path(script_dir).parent)
        if executable is None:
            raise PreviewError(
                f"Danser {DANSER_VERSION} is not installed for previews. "
                "Close Mapperatorinpainter, run 'Install Danser Preview.bat', then reopen it."
            )
        previewer = DanserPreviewer(
            executable=executable,
            beatmapset_root=session.working_directory,
        )
        inpaint_previewers[session_id] = previewer
        return previewer


def _preview_selection_payload(snapshot) -> dict:
    return {
        "session_id": snapshot.session_id,
        "start_time": snapshot.selection_start,
        "end_time": snapshot.selection_end,
        "padding_before": snapshot.padding_before,
        "padding_after": snapshot.padding_after,
        "cursor": snapshot.cursor,
        "configuration_revision": snapshot.configuration_revision,
    }


def _preview_map_cache_entry(session_id: str, session: BeatmapsetSession) -> tuple[str, dict]:
    difficulty = session.active_difficulty
    if difficulty.length_ms is None or difficulty.length_ms <= 0:
        raise ValueError("The active difficulty has no playable hitobjects to preview.")
    revision = session.revision_payload()["current_revision"]
    map_key = f"{session_id}:{difficulty.relative_path}:{revision}"
    with preview_density_cache_lock:
        cached = preview_density_cache.get(map_key)
    if cached is None:
        cached = preview_map_data(difficulty.path, difficulty.length_ms)
        sample_assets = {}
        for event in cached["scene"]["hitsounds"]:
            for sample in event["samples"]:
                if not sample.get("use_map_asset"):
                    continue
                asset_path = _resolve_preview_sample_asset(session, sample["candidate"])
                if asset_path is None:
                    continue
                relative_path = asset_path.relative_to(session.working_directory).as_posix()
                token = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
                sample["asset_token"] = token
                sample_assets[token] = asset_path
        cached["sample_assets"] = sample_assets
        with preview_density_cache_lock:
            preview_density_cache[map_key] = cached
    return map_key, cached


def _resolve_preview_sample_asset(session: BeatmapsetSession, candidate_name: str) -> Path | None:
    candidate_name = (candidate_name or "").strip().strip('"')
    if not candidate_name:
        return None
    normalized = candidate_name.replace("\\", os.sep).replace("/", os.sep)
    relative = Path(normalized)
    if relative.is_absolute() or relative.drive or relative.suffix.casefold() not in {".wav", ".ogg", ".mp3"}:
        return None

    for base in (session.active_difficulty.path.parent, session.working_directory):
        resolved = (base / relative).resolve()
        try:
            resolved.relative_to(session.working_directory)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _preview_scene_payload(session_id: str, map_key: str, cached: dict) -> dict:
    scene = {key: value for key, value in cached["scene"].items() if key != "hitsounds"}
    scene["hitsounds"] = []
    for event in cached["scene"]["hitsounds"]:
        samples = []
        for original_sample in event["samples"]:
            sample = dict(original_sample)
            token = sample.pop("asset_token", None)
            sample.pop("use_map_asset", None)
            if token:
                sample["url"] = url_for(
                    "inpaint_preview_sample",
                    session_id=session_id,
                    token=token,
                    key=map_key,
                )
            samples.append(sample)
        scene["hitsounds"].append({**event, "samples": samples})
    return scene


def _preview_audio_key(session: BeatmapsetSession) -> str:
    audio_path = session.resolve_audio()
    stat = audio_path.stat()
    relative_path = audio_path.relative_to(session.working_directory).as_posix()
    identity = f"{relative_path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def _preview_map_payload(session_id: str, session: BeatmapsetSession) -> dict:
    difficulty = session.active_difficulty
    map_key, cached = _preview_map_cache_entry(session_id, session)
    metadata = cached["metadata"]

    return {
        "key": map_key,
        "relative_path": difficulty.relative_path,
        "version": difficulty.version,
        "mode": difficulty.mode,
        "length_ms": cached["length_ms"],
        "title": metadata["title"],
        "artist": metadata["artist"],
        "mapper": difficulty.mapper,
        "density": cached["density"],
        "object_count": cached["object_count"],
        "audio_url": url_for(
            "inpaint_preview_audio",
            session_id=session_id,
            key=_preview_audio_key(session),
        ),
    }


# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main HTML page."""
    # Jinja rendering is now handled by Flask's render_template
    danser_executable = find_danser_executable(Path(script_dir).parent)
    return render_template(
        'index.html',
        csrf_token=LOCAL_UI_CSRF_TOKEN,
        csrf_header_name=CSRF_HEADER_NAME,
        descriptor_sets=DESCRIPTOR_SETS,
        danser_status={
            'available': danser_executable is not None,
            'version': DANSER_VERSION,
            'path': str(danser_executable) if danser_executable else None,
        },
    )


@app.route('/inpaint/preview-window')
def inpaint_preview_window():
    """Render the persistent preview controller and density seeker."""
    danser_executable = find_danser_executable(Path(script_dir).parent)
    return render_template(
        'preview.html',
        csrf_token=LOCAL_UI_CSRF_TOKEN,
        csrf_header_name=CSRF_HEADER_NAME,
        danser_status={
            'available': danser_executable is not None,
            'version': DANSER_VERSION,
        },
    )


@app.route('/check_bf16_support', methods=['GET'])
def check_bf16_support():
    """Check if the GPU supports bf16 precision for faster inference."""
    try:
        import torch

        if not torch.cuda.is_available():
            return jsonify({"supported": False, "reason": "CUDA not available"})

        # Get GPU compute capability
        device_props = torch.cuda.get_device_properties(0)
        compute_capability = (device_props.major, device_props.minor)
        gpu_name = device_props.name

        # bf16 requires compute capability 8.0+ (Ampere and newer: RTX 30xx, 40xx, A100, etc.)
        supported = compute_capability[0] >= 8

        return jsonify({
            "supported": supported,
            "gpu_name": gpu_name,
            "compute_capability": f"{compute_capability[0]}.{compute_capability[1]}",
            "reason": "GPU supports bf16" if supported else f"GPU compute capability {compute_capability[0]}.{compute_capability[1]} < 8.0 required"
        })
    except Exception as e:
        return jsonify({"supported": False, "reason": str(e)})


@app.route('/start_inference', methods=['POST'])
def start_inference():
    """Starts the inference process based on form data."""
    job_id = uuid.uuid4().hex

    # Create config
    config_name = request.form.get('model')
    with initialize_config_dir(version_base="1.1", config_dir=str(Path(__file__).parent / "configs/inference")):
        cfg = compose(config_name=config_name)
    cfg = OmegaConf.to_object(cfg)
    cfg.use_server = True

    # Required/paths
    cfg.audio_path = request.form.get('audio_path') or None
    cfg.output_path = request.form.get('output_path') or None
    cfg.beatmap_path = request.form.get('beatmap_path') or None
    cfg.lora_path = request.form.get('lora_path') or None

    # Basic settings
    cfg.gamemode = _coerce_optional_int(request.form.get('gamemode')) or 0
    cfg.difficulty = _coerce_optional_float(request.form.get('difficulty'))
    cfg.year = _coerce_optional_int(request.form.get('year'))
    try:
        _validate_year_for_model(config_name, cfg.year)
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400

    # Numeric settings
    cfg.hp_drain_rate = _coerce_optional_float(request.form.get('hp_drain_rate'))
    cfg.circle_size = _coerce_optional_float(request.form.get('circle_size'))
    cfg.overall_difficulty = _coerce_optional_float(request.form.get('overall_difficulty'))
    cfg.approach_rate = _coerce_optional_float(request.form.get('approach_rate'))
    cfg.slider_multiplier = _coerce_optional_float(request.form.get('slider_multiplier'))
    cfg.slider_tick_rate = _coerce_optional_float(request.form.get('slider_tick_rate'))
    cfg.keycount = _coerce_optional_int(request.form.get('keycount'))
    cfg.hold_note_ratio = _coerce_optional_float(request.form.get('hold_note_ratio'))
    cfg.scroll_speed_ratio = _coerce_optional_float(request.form.get('scroll_speed_ratio'))
    cfg.cfg_scale = _coerce_optional_float(request.form.get('cfg_scale')) or cfg.cfg_scale
    cfg.temperature = _coerce_optional_float(request.form.get('temperature')) or cfg.temperature
    cfg.top_p = _coerce_optional_float(request.form.get('top_p')) or cfg.top_p
    cfg.seed = _coerce_optional_int(request.form.get('seed'))
    cfg.mapper_id = _coerce_optional_int(request.form.get('mapper_id'))

    # Metadata
    cfg.title = request.form.get('title') or None
    cfg.title_unicode = request.form.get('title_unicode') or None
    cfg.artist = request.form.get('artist') or None
    cfg.artist_unicode = request.form.get('artist_unicode') or None
    cfg.creator = request.form.get('creator') or None
    cfg.version = request.form.get('version') or None
    cfg.source = request.form.get('source') or None
    cfg.tags = request.form.get('tags') or None
    cfg.preview_time = _coerce_optional_int(request.form.get('preview_time'))

    # Background image
    background_image = request.form.get('background_image')
    if background_image:
        cfg.background = background_image

    # Timing and segmentation
    cfg.start_time = _coerce_optional_int(request.form.get('start_time'))
    cfg.end_time = _coerce_optional_int(request.form.get('end_time'))

    # Checkboxes
    cfg.export_osz = _coerce_bool_checkbox(request.form, 'export_osz')
    cfg.add_to_beatmap = _coerce_bool_checkbox(request.form, 'add_to_beatmap')
    cfg.overwrite_reference_beatmap = _coerce_bool_checkbox(request.form, 'overwrite_reference_beatmap')
    cfg.hitsounded = _coerce_bool_checkbox(request.form, 'hitsounded')
    cfg.super_timing = _coerce_bool_checkbox(request.form, 'super_timing')

    # Precision
    if _coerce_bool_checkbox(request.form, 'enable_bf16'):
        cfg.precision = 'bf16'
    else:
        cfg.precision = 'fp32'

    # Descriptor lists
    descriptors = request.form.getlist('descriptors')
    cfg.descriptors = descriptors if descriptors else None
    negative_descriptors = request.form.getlist('negative_descriptors')
    cfg.negative_descriptors = negative_descriptors if negative_descriptors else None

    # In-context options
    in_context_options = request.form.getlist('in_context_options')
    if in_context_options and cfg.beatmap_path:
        try:
            cfg.in_context = [ContextType[opt] for opt in in_context_options]
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": f"Invalid in-context options: {e}"}), 400

    # Validate and compile args
    try:
        compile_args(cfg, verbose=False)
    except ValueError as ve:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(ve)}), 400

    # Ensure a shared server is running, owned by web UI.
    try:
        _ensure_inference_server(cfg)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Failed to ensure inference server: {e}"}), 500

    # Spawn the worker process.
    try:
        q = mp.Queue()
        p = mp.Process(target=_inference_worker, args=(cfg, q), daemon=True)
        p.start()

        with process_lock:
            processes[job_id] = {"process": p, "queue": q}

        return jsonify({"status": "success", "message": "Inference started", "job_id": job_id}), 202
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Failed to start process: {e}"}), 500


@app.route('/inpaint/open', methods=['POST'])
def open_inpaint_session():
    """Copy an immutable `.osz` or song folder and return its difficulties."""
    source_path = (request.form.get('path') or '').strip()
    if not source_path:
        return jsonify({"status": "error", "message": "Choose an .osz beatmapset or song folder first."}), 400

    session = None
    session_id = None
    try:
        session = BeatmapsetSession.open(source_path)
        session_id = uuid.uuid4().hex
        payload = session_payload(session_id, session)
        with inpaint_sessions_lock:
            inpaint_sessions[session_id] = session
        return jsonify({"status": "success", "session": payload})
    except Exception as exc:
        if session is not None:
            with inpaint_sessions_lock:
                if session_id is not None:
                    inpaint_sessions.pop(session_id, None)
            session.cleanup()
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/select-difficulty', methods=['POST'])
def select_inpaint_difficulty():
    session_id = (request.form.get('session_id') or '').strip()
    previous_difficulty = None
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("Wait for the current regeneration to finish before changing difficulty.")
        previous_difficulty = session.active_difficulty
        session.select_difficulty(request.form.get('relative_path') or '')
        return jsonify({"status": "success", "session": session_payload(session_id, session)})
    except Exception as exc:
        if previous_difficulty is not None:
            session.active_difficulty = previous_difficulty
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/start', methods=['POST'])
def start_inpaint():
    """Start one transactional interval regeneration through the shared inference server."""
    session_id = (request.form.get('session_id') or '').strip()
    model_name = (request.form.get('model') or 'v32').strip()
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("This Inpaint session already has a regeneration running.")

        values = {key: request.form.get(key, '') for key in request.form.keys()}
        values['timing_context'] = 'true' if 'timing_context' in request.form else 'false'
        cfg = compose_inpainting_config(
            config_dir=Path(__file__).parent / "configs/inference",
            model_name=model_name,
            session=session,
            values=values,
            descriptors=request.form.getlist('descriptors'),
            negative_descriptors=request.form.getlist('negative_descriptors'),
        )
        _validate_year_for_model(model_name, cfg.year)
        compile_args(cfg, verbose=False)
        cfg.use_server = True
        _ensure_inference_server(cfg)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Could not prepare Inpaint generation: {exc}"}), 500

    try:
        job_id = uuid.uuid4().hex
        q = mp.Queue()
        success_event = mp.Event()
        active_path = session.active_difficulty.path
        snapshot = active_path.read_bytes()
        process = mp.Process(
            target=_inpaint_worker,
            args=(cfg, session, q, success_event),
            daemon=True,
        )
        process.start()
        with process_lock:
            processes[job_id] = {
                "process": process,
                "queue": q,
                "inpaint_session_id": session_id,
                "inpaint_path": active_path,
                "inpaint_snapshot": snapshot,
                "inpaint_revision_metadata": generation_revision_metadata(cfg),
                "success_event": success_event,
            }
        return jsonify({
            "status": "success",
            "message": "Inpaint regeneration started",
            "job_id": job_id,
            "seed": cfg.seed,
            "start_time": cfg.start_time,
            "end_time": cfg.end_time,
        }), 202
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Failed to start Inpaint process: {exc}"}), 500


@app.route('/inpaint/export', methods=['POST'])
def export_inpaint_session():
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("Wait for regeneration to finish before exporting.")
        requested_destination = (request.form.get('destination') or '').strip()
        if not requested_destination:
            raise ValueError("Choose an export destination first.")
        destination = session.export(
            requested_destination,
            overwrite=(request.form.get('overwrite') == 'true'),
        )
        return jsonify({
            "status": "success",
            "path": str(destination),
            "session": session_payload(session_id, session),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/preview', methods=['POST'])
def preview_inpaint_session():
    """Launch the active standard difficulty around the regeneration interval."""
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("Wait for regeneration to finish before previewing.")
        if session.active_difficulty.mode != 0:
            raise ValueError("Danser previews support only osu!standard difficulties (mode 0).")

        selected_start = parse_timestamp_ms(request.form.get('start_time') or '')
        selected_end = parse_timestamp_ms(request.form.get('end_time') or '')
        if selected_end <= selected_start:
            raise ValueError("Preview end time must be after its start time.")

        before_seconds = float(request.form.get('padding_before') or '3')
        after_seconds = float(request.form.get('padding_after') or '3')
        if not 0 <= before_seconds <= 30 or not 0 <= after_seconds <= 30:
            raise ValueError("Preview padding must be between 0 and 30 seconds.")

        map_length = session.active_difficulty.length_ms
        if map_length is not None and selected_end > map_length:
            raise ValueError("The selected interval extends beyond the active difficulty.")
        preview_start = max(0, selected_start - round(before_seconds * 1_000))
        preview_end = selected_end + round(after_seconds * 1_000)
        if map_length is not None:
            preview_end = min(preview_end, map_length)

        previewer = _get_inpaint_previewer(session_id, session)
        launched = previewer.preview(
            session.active_difficulty.path,
            preview_start,
            preview_end,
        )
        return jsonify({
            "status": "success",
            "viewer": launched.viewer,
            "process_id": launched.process_id,
            "start_time": launched.start_time,
            "end_time": launched.end_time,
            "difficulty": session.active_difficulty.version,
        })
    except (ValueError, PreviewError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Could not launch preview: {exc}"}), 500


@app.route('/inpaint/preview-window/config', methods=['POST'])
def configure_inpaint_preview_window():
    """Synchronize editor selection with the persistent preview controller."""
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        start = parse_timestamp_ms(request.form.get('start_time') or '')
        end = parse_timestamp_ms(request.form.get('end_time') or '')
        if end <= start:
            raise ValueError("Preview end time must be after its start time.")
        padding_before = round(float(request.form.get('padding_before') or '3') * 1_000)
        padding_after = round(float(request.form.get('padding_after') or '3') * 1_000)
        previous_session_id = preview_window_controller.snapshot().session_id
        snapshot = preview_window_controller.configure(
            session_id=session_id,
            selection_start=start,
            selection_end=end,
            padding_before=padding_before,
            padding_after=padding_after,
            length_ms=session.active_difficulty.length_ms,
        )
        if previous_session_id and previous_session_id != session_id:
            _close_inpaint_previewer(previous_session_id)
        return jsonify({"status": "success", "selection": _preview_selection_payload(snapshot)})
    except (ValueError, PreviewError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/preview-window/state', methods=['POST'])
def get_inpaint_preview_window_state():
    """Return current map/revision data for the persistent preview window."""
    snapshot = preview_window_controller.snapshot()
    if not snapshot.session_id:
        return jsonify({
            "status": "success",
            "has_session": False,
            "selection": _preview_selection_payload(snapshot),
            "danser_available": find_danser_executable(Path(script_dir).parent) is not None,
        })

    try:
        session = _get_inpaint_session(snapshot.session_id)
    except ValueError:
        cleared = preview_window_controller.clear_session(snapshot.session_id)
        return jsonify({
            "status": "success",
            "has_session": False,
            "selection": _preview_selection_payload(cleared),
            "danser_available": find_danser_executable(Path(script_dir).parent) is not None,
        })

    try:
        map_payload = _preview_map_payload(snapshot.session_id, session)
        length_ms = map_payload["length_ms"]
        if length_ms is not None and (
            snapshot.selection_start >= length_ms or snapshot.selection_end > length_ms
        ):
            snapshot = preview_window_controller.configure(
                session_id=snapshot.session_id,
                selection_start=snapshot.selection_start,
                selection_end=snapshot.selection_end,
                padding_before=snapshot.padding_before,
                padding_after=snapshot.padding_after,
                length_ms=length_ms,
            )
        return jsonify({
            "status": "success",
            "has_session": True,
            "selection": _preview_selection_payload(snapshot),
            "map": map_payload,
            "generating": _session_has_active_job(snapshot.session_id),
            "danser_available": find_danser_executable(Path(script_dir).parent) is not None,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/preview-window/data', methods=['POST'])
def get_inpaint_preview_window_data():
    """Return parsed playfield data only when the preview's map revision changes."""
    snapshot = preview_window_controller.snapshot()
    try:
        if not snapshot.session_id:
            raise ValueError("Open a beatmapset in Inpaint first.")
        session = _get_inpaint_session(snapshot.session_id)
        map_key, cached = _preview_map_cache_entry(snapshot.session_id, session)
        requested_key = (request.form.get('key') or '').strip()
        if requested_key and requested_key != map_key:
            raise ValueError("The active map changed while the preview was loading. Please retry.")
        return jsonify({
            "status": "success",
            "key": map_key,
            "scene": _preview_scene_payload(snapshot.session_id, map_key, cached),
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/preview-window/audio/<session_id>')
def inpaint_preview_audio(session_id: str):
    """Stream only the selected difficulty's resolved audio to the embedded player."""
    snapshot = preview_window_controller.snapshot()
    try:
        if snapshot.session_id != session_id:
            raise ValueError("This preview session is no longer active.")
        session = _get_inpaint_session(session_id)
        if request.args.get('key') != _preview_audio_key(session):
            raise ValueError("This preview audio URL is stale.")
        return send_file(session.resolve_audio(), conditional=True, max_age=0)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route('/inpaint/preview-window/sample/<session_id>/<token>')
def inpaint_preview_sample(session_id: str, token: str):
    """Stream a parser-resolved, in-session custom hitsound sample."""
    snapshot = preview_window_controller.snapshot()
    try:
        if snapshot.session_id != session_id:
            raise ValueError("This preview session is no longer active.")
        session = _get_inpaint_session(session_id)
        map_key, cached = _preview_map_cache_entry(session_id, session)
        if request.args.get('key') != map_key:
            raise ValueError("This preview sample URL is stale.")
        asset_path = cached.get("sample_assets", {}).get(token)
        if asset_path is None:
            raise ValueError("This preview sample does not exist.")
        return send_file(asset_path, conditional=True, max_age=0)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route('/inpaint/preview-window/play', methods=['POST'])
def play_inpaint_preview_window():
    """Launch the optional high-fidelity Danser check at the preview cursor."""
    snapshot = preview_window_controller.snapshot()
    try:
        if not snapshot.session_id:
            raise ValueError("Open a beatmapset in Inpaint first.")
        session = _get_inpaint_session(snapshot.session_id)
        if _session_has_active_job(snapshot.session_id):
            raise ValueError("Wait for regeneration to finish before previewing.")
        if session.active_difficulty.mode != 0:
            raise ValueError("Danser previews support only osu!standard difficulties (mode 0).")
        _, cached = _preview_map_cache_entry(snapshot.session_id, session)
        length_ms = cached["length_ms"]
        if length_ms is None or length_ms <= 0:
            raise ValueError("The active difficulty has no playable hitobjects to preview.")
        cursor = int(request.form.get('cursor') or snapshot.cursor)
        cursor = max(0, min(cursor, length_ms - 1))
        previewer = _get_inpaint_previewer(snapshot.session_id, session)
        launched = previewer.preview(
            session.active_difficulty.path,
            cursor,
            length_ms + 1,
        )
        snapshot = preview_window_controller.set_cursor(cursor, length_ms=length_ms)
        return jsonify({
            "status": "success",
            "viewer": launched.viewer,
            "process_id": launched.process_id,
            "cursor": snapshot.cursor,
            "difficulty": session.active_difficulty.version,
        })
    except (ValueError, PreviewError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Could not launch preview: {exc}"}), 500


@app.route('/inpaint/preview-window/selection', methods=['POST'])
def copy_inpaint_preview_boundary():
    """Copy the preview cursor into the editor's regeneration start or end."""
    snapshot = preview_window_controller.snapshot()
    try:
        if not snapshot.session_id:
            raise ValueError("Open a beatmapset in Inpaint first.")
        session = _get_inpaint_session(snapshot.session_id)
        _, cached = _preview_map_cache_entry(snapshot.session_id, session)
        length_ms = cached["length_ms"]
        if length_ms <= 0:
            raise ValueError("The active difficulty has no playable hitobjects.")
        boundary = (request.form.get('boundary') or '').strip().lower()
        timestamp = int(request.form.get('cursor') or snapshot.cursor)
        updated = preview_window_controller.copy_boundary(
            boundary,
            timestamp,
            length_ms=length_ms,
        )
        return jsonify({"status": "success", "selection": _preview_selection_payload(updated)})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/preview-window/stop', methods=['POST'])
def stop_inpaint_preview_window():
    snapshot = preview_window_controller.snapshot()
    if snapshot.session_id:
        with inpaint_previewers_lock:
            previewer = inpaint_previewers.get(snapshot.session_id)
        if previewer is not None:
            previewer.stop()
    return jsonify({"status": "success"})


@app.route('/inpaint/state', methods=['POST'])
def inpaint_session_state():
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        return jsonify({"status": "success", "session": session_payload(session_id, session)})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/undo', methods=['POST'])
def undo_inpaint_revision():
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("Wait for regeneration to finish before undoing.")
        session.undo()
        return jsonify({"status": "success", "session": session_payload(session_id, session)})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/redo', methods=['POST'])
def redo_inpaint_revision():
    session_id = (request.form.get('session_id') or '').strip()
    try:
        session = _get_inpaint_session(session_id)
        if _session_has_active_job(session_id):
            raise ValueError("Wait for regeneration to finish before redoing.")
        session.redo()
        return jsonify({"status": "success", "session": session_payload(session_id, session)})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/inpaint/close', methods=['POST'])
def close_inpaint_session():
    session_id = (request.form.get('session_id') or '').strip()
    try:
        if _session_has_active_job(session_id):
            raise ValueError("Wait for regeneration to finish before closing the session.")
        with inpaint_sessions_lock:
            session = inpaint_sessions.get(session_id)
            if session is not None and session.dirty and request.form.get('discard') != 'true':
                return jsonify({
                    "status": "error",
                    "message": "The Inpaint session has unsaved generations.",
                    "unsaved_changes": True,
                }), 409
            session = inpaint_sessions.pop(session_id, None)
        if session is not None:
            _close_inpaint_previewer(session_id)
            preview_window_controller.clear_session(session_id)
            with preview_density_cache_lock:
                stale_keys = [key for key in preview_density_cache if key.startswith(f"{session_id}:")]
                for key in stale_keys:
                    preview_density_cache.pop(key, None)
            session.cleanup()
        return jsonify({"status": "success"})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/stream_output')
def stream_output():
    """Streams the output of the running inference process using SSE."""

    job_id = request.args.get('job_id', '').strip()
    if not job_id:
        return Response("event: end\ndata: Missing job_id\n\n", mimetype='text/event-stream')

    def generate():
        with process_lock:
            rec = processes.get(job_id)
            if not rec:
                yield "event: end\ndata: No active process or process already finished\n\n"
                return
            proc = rec["process"]
            q = rec["queue"]

        full_output_lines = []
        error_occurred = False
        exit_code = None

        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue_mod.Empty:
                    if not proc.is_alive():
                        # Process died without sending sentinel.
                        exit_code = proc.exitcode
                        break
                    continue

                if isinstance(item, dict) and item.get("_event") == "exit":
                    exit_code = item.get("code", 0)
                    break

                line = str(item)
                full_output_lines.append(line + "\n")
                yield f"data: {line.rstrip()}\n\n"
                sys.stdout.flush()

            # Determine error state.
            if exit_code and exit_code != 0:
                with process_lock:
                    was_cancelled = job_id in cancelled_jobs
                    cancelled_jobs.discard(job_id)
                if was_cancelled:
                    error_occurred = False
                else:
                    error_occurred = True
        except Exception as e:
            error_occurred = True
            full_output_lines.append(f"\n--- STREAMING ERROR ---\n{e}\n")
        finally:
            # Synchronize a child-process transaction into the parent-owned session.
            # Forced cancellation cannot execute the child's exception handler, so
            # the parent also owns a pristine snapshot for atomic rollback.
            session_id = rec.get("inpaint_session_id")
            if session_id:
                succeeded = exit_code == 0 and rec["success_event"].is_set()
                try:
                    session = _get_inpaint_session(session_id)
                    if succeeded:
                        session.record_revision(
                            path=rec["inpaint_path"],
                            metadata=rec["inpaint_revision_metadata"],
                        )
                        try:
                            automatic_output = session.export(
                                session.next_export_path(INPAINT_OUTPUT_DIRECTORY)
                            )
                            yield f"event: inpaint_export\ndata: {automatic_output}\n\n"
                        except Exception as export_exc:
                            traceback.print_exc()
                            yield f"event: inpaint_export_error\ndata: {export_exc}\n\n"
                    else:
                        restore_snapshot(rec["inpaint_path"], rec["inpaint_snapshot"])
                except Exception:
                    traceback.print_exc()

            # Save logs on error (same behavior as before).
            if error_occurred:
                try:
                    log_dir = os.path.join(script_dir, 'logs')
                    os.makedirs(log_dir, exist_ok=True)
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    pid = proc.pid if proc is not None else 0
                    log_filename = f"error_{pid}_{timestamp}.log"
                    log_filepath = os.path.join(log_dir, log_filename)
                    error_content = "".join(full_output_lines)

                    with open(log_filepath, 'w', encoding='utf-8') as f:
                        f.write(error_content)
                    yield f"event: error_log\ndata: {log_filepath.replace(os.sep, '/')}\n\n"
                except Exception:
                    pass

            completion_message = "Process completed"
            if error_occurred:
                completion_message += " with errors"
            yield f"event: end\ndata: {completion_message}\n\n"

            # Cleanup.
            with process_lock:
                processes.pop(job_id, None)
                cancelled_jobs.discard(job_id)

            try:
                if proc is not None:
                    proc.join(timeout=1)
            except Exception:
                pass

            try:
                q.cancel_join_thread()
            except Exception:
                pass

            try:
                q.close()
            except Exception:
                pass

    return Response(generate(), mimetype='text/event-stream')


@app.route('/cancel_inference', methods=['POST'])
def cancel_inference():
    """Attempts to terminate the currently running inference process."""
    job_id = request.form.get('job_id', '').strip()
    if not job_id:
        return jsonify({"status": "error", "message": "Missing job_id"}), 400

    with process_lock:
        rec = processes.get(job_id)
        if not rec:
            return jsonify({"status": "error", "message": "No active process found"}), 404
        proc = rec["process"]

        if proc.is_alive():
            cancelled_jobs.add(job_id)
            try:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
                else:
                    proc.terminate()
                return jsonify({"status": "success", "message": "Cancel request sent"}), 200
            except Exception as e:
                return jsonify({"status": "error", "message": f"Failed to terminate process: {e}"}), 500

    return jsonify({"status": "success", "message": "Process already finished"}), 200


@app.route('/open_folder', methods=['POST'])
def open_folder():
    """Opens a folder in the file explorer."""
    folder_path = request.form.get('folder')
    print(f"Request received to open folder: {folder_path}")
    if not folder_path:
        return jsonify({"status": "error", "message": "No folder path specified"}), 400

    # Resolve to absolute path for checks
    abs_folder_path = os.path.abspath(folder_path)

    # Security check: Basic check if it's within the project directory.
    # Adjust this check based on your security needs and where output is expected.
    workspace_root = os.path.abspath(script_dir)
    # Example: Only allow opening if it's inside the workspace root
    # if not abs_folder_path.startswith(workspace_root):
    #     print(f"Security Warning: Attempt to open potentially restricted folder: {abs_folder_path}")
    #     return jsonify({"status": "error", "message": "Access denied to specified folder path."}), 403

    if not os.path.isdir(abs_folder_path):
        print(f"Invalid folder path provided or folder does not exist: {abs_folder_path}")
        return jsonify({"status": "error", "message": "Invalid or non-existent folder path specified"}), 400

    try:
        system = platform.system()
        if system == 'Windows':
            os.startfile(os.path.normpath(abs_folder_path))
        elif system == 'Darwin':
            subprocess.Popen(['open', abs_folder_path])
        else:
            subprocess.Popen(['xdg-open', abs_folder_path])
        print(f"Successfully requested to open folder: {abs_folder_path}")
        return jsonify({"status": "success", "message": "Folder open request sent."}), 200
    except Exception as e:
        print(f"Error opening folder '{abs_folder_path}': {e}")
        return jsonify({"status": "error", "message": f"Could not open folder: {e}"}), 500


@app.route('/open_log_file', methods=['POST'])
def open_log_file():
    """Opens a specific log file."""
    log_path = request.form.get('path')
    print(f"Request received to open log file: {log_path}")
    if not log_path:
        return jsonify({"status": "error", "message": "No log file path specified"}), 400

    # Security Check: Ensure the file is within the 'logs' directory
    log_dir = os.path.abspath(os.path.join(script_dir, 'logs'))
    # Normalize the input path and resolve symlinks etc.
    abs_log_path = os.path.abspath(os.path.normpath(log_path))

    # IMPORTANT SECURITY CHECK:
    if not abs_log_path.startswith(log_dir + os.sep):
        print(f"Security Alert: Attempt to open file outside of logs directory: {abs_log_path} (Log dir: {log_dir})")
        return jsonify({"status": "error", "message": "Access denied: File is outside the designated logs directory."}), 403

    if not os.path.isfile(abs_log_path):
        print(f"Log file not found at: {abs_log_path}")
        return jsonify({"status": "error", "message": "Log file not found."}), 404

    try:
        system = platform.system()
        if system == 'Windows':
            os.startfile(abs_log_path) # normpath already applied
        elif system == 'Darwin':
            subprocess.Popen(['open', abs_log_path])
        else:
            subprocess.Popen(['xdg-open', abs_log_path])
        print(f"Successfully requested to open log file: {abs_log_path}")
        return jsonify({"status": "success", "message": "Log file open request sent."}), 200
    except Exception as e:
        print(f"Error opening log file '{abs_log_path}': {e}")
        return jsonify({"status": "error", "message": f"Could not open log file: {e}"}), 500


@app.route('/save_config', methods=['POST'])
def save_config():
    try:
        file_path = request.form.get('file_path')
        config_data = request.form.get('config_data')

        if not file_path or not config_data:
            return jsonify({'success': False, 'error': 'Missing required parameters'})

        # Write the configuration file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(config_data)

        return jsonify({
            'success': True,
            'file_path': file_path,
            'message': 'Configuration saved successfully'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Failed to save configuration: {str(e)}'
        })


@app.route('/validate_paths', methods=['POST'])
def validate_paths():
    """Validates and autofills missing paths."""
    try:
        # Get paths
        audio_path = request.form.get('audio_path', '').strip()
        beatmap_path = request.form.get('beatmap_path', '').strip()
        output_path = request.form.get('output_path', '').strip()

        inference_args = InferenceConfig()
        inference_args.audio_path = audio_path
        inference_args.beatmap_path = beatmap_path
        inference_args.output_path = output_path

        try:
            compile_args(inference_args, verbose=False)
        except ValueError as v:
            return jsonify({
                'success': False,
                'autofilled_args': None,
                'errors': [str(v)]
            }), 200

        autofilled_args = asdict(inference_args)
        del autofilled_args['in_context']
        del autofilled_args['output_type']
        del autofilled_args['train']

        # Return the results
        response_data = {
            'success': True,
            'autofilled_args': autofilled_args,
            'errors': []
        }

        return jsonify(response_data), 200

    except Exception as e:
        error_msg = f"Error during path validation: {str(e)}"
        print(error_msg)
        return jsonify({
            'success': False,
            'autofilled_args': None,
            'errors': [error_msg]
        }), 500


# --- Function to Run Flask in a Thread ---
def run_flask(port):
    """Runs the Flask app."""

    # Use threaded=True for better concurrency within Flask
    # Avoid debug=True as it interferes with threading and pywebview
    print(f"Starting Flask server on http://127.0.0.1:{port}")
    try:
        # Explicitly set debug=False, in addition to FLASK_ENV=production
        app.run(host='127.0.0.1', port=port, threaded=True, debug=False)
    except OSError as e:
        print(f"Flask server could not start on port {port}: {e}")
        # Optionally: try another port or exit


# --- Function to Find Available Port ---
def find_available_port(start_port=5000, max_tries=100):
    """Finds an available TCP port."""
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                print(f"Found available port: {port}")
                return port
            except OSError:
                continue  # Port already in use
    raise IOError("Could not find an available port.")


def launch_browser_fallback(flask_url, flask_thread):
    """Keep the server alive when an embedded window cannot be created."""
    print(f"Running without an embedded window. Open {flask_url} in your browser.")
    print("Press Ctrl+C to stop the server.")

    try:
        while flask_thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        _shutdown_application_resources()


def launch_webview_window(window_title, flask_url, window_width, window_height, api):
    """Create the embedded pywebview window when a GUI backend is available."""
    print(f"Creating pywebview window loading URL: {flask_url}")
    try:
        webview.create_window(
            window_title,
            url=flask_url,
            width=window_width,
            height=window_height,
            resizable=True,
            js_api=api,
        )
        webview.start()
        print("Pywebview window closed. Shutting down application resources...")
        _shutdown_application_resources()
        print("Application shutdown complete. Exiting.")
        return True
    except Exception as e:
        print(f"pywebview could not start an embedded window: {e}")
        print(traceback.format_exc())
        return False


# --- Main Execution ---
if __name__ == '__main__':
    # Use spawn instead of fork to avoid issues with CUDA on Linux
    multiprocessing.set_start_method('spawn', force=True)
    # Find an available port for Flask
    flask_port = find_available_port()

    # Start Flask server in a daemon thread
    flask_thread = threading.Thread(target=run_flask, args=(flask_port,), daemon=True)
    flask_thread.start()

    # Give Flask a moment to start up
    time.sleep(1)

    # --- Calculate Responsive Window Size ---
    try:
        primary_screen = webview.screens[0]
        screen_width = primary_screen.width
        screen_height = primary_screen.height
        # Calculate window size (e.g., 45% width, 95% height of primary screen)
        window_width = int(screen_width * 0.45)
        window_height = int(screen_height * 0.9)
        print(f"Screen: {screen_width}x{screen_height}, Window: {window_width}x{window_height}")
    except Exception as e:
        print(f"Could not get screen dimensions, using default: {e}")
        # Fallback to default size if screen info is unavailable
        window_width = 900
        window_height = 1000
    # --- End Calculate Responsive Window Size ---

    # Create the pywebview window pointing to the Flask server
    window_title = 'Mapperatorinator'
    flask_url = f'http://127.0.0.1:{flask_port}/'
    application_base_url = flask_url

    # Instantiate the API class (doesn't need window object anymore)
    api = Api()

    if not launch_webview_window(window_title, flask_url, window_width, window_height, api):
        launch_browser_fallback(flask_url, flask_thread)
