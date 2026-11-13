import os
import json
import hashlib
import time
from pathlib import Path
from typing import Optional

import requests
import gdown
import joblib

from util.logger import Logger
from config import cfg


logger = Logger("Downloader")

# Laedt die Mirror Config aus dem Root des Projekts
def _load_mirrors_config() -> dict:
    possible_paths = [
        Path("/app/mirrors.json"),  
        # wenn nicht docker dann hier hardcoded
        Path(__file__).parent.parent.parent.parent.parent.parent / "mirrors.json",  
    ]
    # FallBack-Mechanismus: Versuche verschiedene Pfade, um die Konfigurationsdatei zu finden
    for config_path in possible_paths:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                logger.info(f"Loaded mirrors config from {config_path}")
                return json.load(f)
    logger.warning("mirrors.json not found, using defaults")
    return {"files": {}, "download_settings": {}}


MIRRORS_CONFIG = _load_mirrors_config()
DOWNLOAD_SETTINGS = MIRRORS_CONFIG.get("download_settings", {})
FILES_CONFIG = MIRRORS_CONFIG.get("files", {})


def _data_dir() -> Path:
    """Resolve the main data directory, ensuring it exists."""
    d = Path(cfg.get("semanticpaper", "data_dir", os.getenv("DATA_DIR", "/app/data")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _models_dir() -> Path:
    """Resolve the models sub-directory, ensuring it exists."""
    d = _data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _part_path(dest: Path) -> Path:
    """Temporary file used while downloading; renamed to `dest` only when complete."""
    return dest.with_name(dest.name + ".part")


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_cache_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256.json")


def _file_stamp(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _cached_sha256(path: Path) -> str | None:
    """sha256 verified earlier, if the file is unchanged since (same size + mtime)."""
    try:
        cached = json.loads(_hash_cache_path(path).read_text(encoding="utf-8"))
        stamp = _file_stamp(path)
        if cached.get("size") == stamp["size"] and cached.get("mtime_ns") == stamp["mtime_ns"]:
            return cached.get("sha256")
    except Exception:
        pass
    return None


def _store_sha256(path: Path, digest: str):
    try:
        _hash_cache_path(path).write_text(json.dumps({**_file_stamp(path), "sha256": digest}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not cache checksum of {path}: {e}")


def _checksum_ok(path: Path, expected_sha256: str | None, description: str) -> bool:
    """
    Verifies the sha256 from mirrors.json (key "sha256"), if configured.
    Important for the .pkl models: unpickling a manipulated file executes code.
    A successful verification is cached next to the file (keyed by size +
    mtime), so the multi-GB UMAP model is not re-hashed on every start.
    """
    if not expected_sha256:
        logger.warning(f"No sha256 configured for {description} in mirrors.json – integrity not verified.")
        return True
    if _cached_sha256(path) == expected_sha256:
        logger.info(f"Checksum OK for {description} (cached, file unchanged)")
        return True
    actual = _sha256(path)
    if actual != expected_sha256:
        logger.error(f"Checksum mismatch for {description}: expected {expected_sha256}, got {actual}")
        return False
    _store_sha256(path, actual)
    logger.info(f"Checksum OK for {description}")
    return True


def _total_from_content_range(value) -> int:
    """'bytes 100-199/1000' -> 1000 (0 if unknown)."""
    try:
        return int(str(value).rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return 0


def _get_file_config(file_key: str) -> dict:
    """Get configuration for a specific file from mirrors.json."""
    return FILES_CONFIG.get(file_key, {})

"""
Abstract: Downloads a file from an HTTP mirror using streaming.
        Data goes to ``<dest>.part`` and is renamed only when complete. If a
        .part file from an interrupted attempt exists, the download resumes
        with an HTTP Range request (falls back to a full download when the
        server ignores Range).
        Args:
        - url: Direct HTTP URL
        - dest: Local destination path
        - description: Human-readable label for log messages
        Returns: bool -> True if download succeeded
"""
def _download_http(url: str, dest: Path, description: str, _allow_restart: bool = True) -> bool:

    timeout = DOWNLOAD_SETTINGS.get("timeout_seconds", 300)
    chunk_size = DOWNLOAD_SETTINGS.get("chunk_size_bytes", 8 * 1024 * 1024)
    part = _part_path(dest)

    try:
        resume_from = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
        if resume_from:
            logger.info(f"Resuming {description} from {url} at {resume_from / (1024**2):.1f} MB...")
        else:
            logger.info(f"Downloading {description} from {url}...")
        response = requests.get(url, stream=True, timeout=timeout, headers=headers)
        status = getattr(response, "status_code", 200)

        if resume_from and status == 416:
            # Range not satisfiable: the .part does not fit this file – start over
            logger.warning(f"Cannot resume {description} (HTTP 416) – restarting download")
            part.unlink(missing_ok=True)
            return _download_http(url, dest, description, _allow_restart=False) if _allow_restart else False
        response.raise_for_status()

        if resume_from and status == 206:
            mode = "ab"
            downloaded = resume_from
            total_size = _total_from_content_range(response.headers.get("content-range")) \
                or resume_from + int(response.headers.get("content-length", 0))
        else:
            if resume_from:
                logger.info(f"Server ignored the Range request – downloading {description} from scratch")
            mode = "wb"
            downloaded = 0
            total_size = int(response.headers.get("content-length", 0))

        last_log_percent = (downloaded * 100 // total_size) if total_size else -10
        # streamed den Download in Chunks, damit wir große Dateien herunterladen können ohne zu viel RAM zu verbrauchen.
        # Geschrieben wird in eine .part-Datei, die erst nach vollständigem Download umbenannt wird –
        # ein abgebrochener Download hinterlässt so nie eine Datei, die beim nächsten Start als "fertig" gilt.
        with open(part, mode) as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = int(downloaded * 100 / total_size)
                        if percent >= last_log_percent + 10:
                            logger.info(f"  {description}: {percent}% ({downloaded / (1024**2):.1f} MB / {total_size / (1024**2):.1f} MB)")
                            last_log_percent = percent
        size = part.stat().st_size if part.exists() else 0
        if size == 0:
            logger.error(f"Download resulted in empty file: {dest}")
            part.unlink(missing_ok=True)
            return False
        if total_size > 0 and size > total_size:
            logger.error(f"Download of {description} is larger than expected ({size:,} > {total_size:,} bytes) – discarding")
            part.unlink(missing_ok=True)
            return False
        if total_size > 0 and size < total_size:
            # keep the .part: the next attempt resumes from here
            logger.error(f"Incomplete download of {description}: {size:,} of {total_size:,} bytes (will resume)")
            return False
        os.replace(part, dest)
        _hash_cache_path(dest).unlink(missing_ok=True)
        logger.info(f"Downloaded {description}: {size:,} bytes  {dest}")
        return True

    except requests.exceptions.RequestException as e:
        # .part is kept on purpose so the next attempt can resume
        logger.warning(f"HTTP download failed for {description} from {url}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error downloading {description}: {e}")
        return False

"""
    Abstract: Downloads a single file from Google Drive (fallback method).
              Retries on transient failures (e.g. Google Drive rate limits).
    Args:
    - file_id: Google Drive file ID
    - dest: Local destination path
    - description: Human-readable label for log messages
    - max_retries: Maximum number of retry attempts
    - retry_delay: Seconds to wait between retries (doubles each attempt)
    Returns: bool -> True if download succeeded and file is non-empty
"""
def _download_gdrive(file_id: str, dest: Path, description: str, max_retries: int = 3, retry_delay: int = 60) -> bool:
    # Fallback falls kein HTTP Mirror gegeben wird - dann ists noch auf meiner Google Drive 
    url = f"https://drive.google.com/uc?id={file_id}"
    delay = retry_delay

    for attempt in range(1, max_retries + 1):
        logger.info(f"Downloading {description} from Google Drive (attempt {attempt}/{max_retries})...")
        try:
            part = _part_path(dest)
            output = gdown.download(url, str(part), quiet=False, fuzzy=True)
            if output is None or not part.exists():
                logger.error(f"GDrive download failed for {description}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                return False
            size = part.stat().st_size
            if size == 0:
                logger.error(f"Downloaded file {dest} is empty!")
                part.unlink(missing_ok=True)
                if attempt < max_retries:
                    logger.info(f"Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                return False
            os.replace(part, dest)
            logger.info(f"Downloaded {description} from GDrive: {size:,} bytes  {dest}")
            return True
        except Exception as e:
            logger.error(f"Error downloading {description} from GDrive: {e}")
            if attempt < max_retries:
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                return False
    return False

"""
    Abstract: Downloads a file using the mirror-first strategy.
              1. Try each HTTP mirror in order
              2. Fall back to Google Drive if all mirrors fail
    Args:
    - file_key: Key in mirrors.json (e.g., "dataset", "pca_model", "umap_model")
    - dest: Local destination path
    Returns: bool -> True if download succeeded
"""
def _download_file(file_key: str, dest: Path) -> bool:
    # Liest die Konfiguration für die angegebene Datei aus mirrors.json, einschließlich Beschreibung, HTTP-Mirrors und optionaler Google Drive Fallback-Informationen. Überprüft zuerst, ob die Datei bereits existiert und nicht leer ist, um unnötige Downloads zu vermeiden. Versucht dann, die Datei von jedem HTTP-Mirror herunterzuladen, mit konfigurierbaren Wiederholungsversuchen und Verzögerungen. Wenn alle HTTP-Mirrors fehlschlagen, versucht es den Download von Google Drive mit ähnlichen Wiederholungsmechanismen. Gibt True zurück, wenn der Download erfolgreich war und die Datei existiert, andernfalls False.
    config = _get_file_config(file_key)
    if not config:
        logger.error(f"No configuration found for file: {file_key}")
        return False
    # Beschreibung, die in Log-Nachrichten verwendet wird, z.B. "Dataset", "PCA model", "UMAP model"
    description = config.get("description", file_key)
    mirrors = config.get("mirrors", [])
    gdrive_fallback = config.get("gdrive_fallback", {})
    
    expected_sha256 = (config.get("sha256") or "").strip().lower() or None

    # Vorhandene Datei nur weiterverwenden, wenn sie vollständig ist. Downloads landen erst
    # nach Abschluss unter dem endgültigen Namen; mit sha256 in mirrors.json wird zusätzlich geprüft.
    if dest.exists() and dest.stat().st_size > 0:
        if _checksum_ok(dest, expected_sha256, description):
            logger.info(f"{description} already exists at {dest} ({dest.stat().st_size:,} bytes), skipping download.")
            return True
        logger.warning(f"{description} at {dest} failed the checksum – downloading again.")
        dest.unlink(missing_ok=True)

    max_retries = DOWNLOAD_SETTINGS.get("max_retries", 3)
    retry_delay = DOWNLOAD_SETTINGS.get("retry_delay_seconds", 10)
    
    # versucht zuerst die HTTP-Mirrors in der angegebenen Reihenfolge, mit Wiederholungsversuchen bei Fehlern (z.B. Netzwerkprobleme, Mirror-Ausfälle)
    for mirror_url in mirrors:
        for attempt in range(1, max_retries + 1):
            if _download_http(mirror_url, dest, description):
                if _checksum_ok(dest, expected_sha256, description):
                    return True
                dest.unlink(missing_ok=True)
            if attempt < max_retries:
                logger.info(f"Mirror attempt {attempt}/{max_retries} failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
        logger.warning(f"Mirror {mirror_url} exhausted, trying next...")
    
    # dann fallback auf Google Drive, wenn alle HTTP-Mirrors fehlschlagen. Auch hier mit Wiederholungsversuchen, da Google Drive manchmal unzuverlässig sein kann (z.B. bei großen Dateien oder wenn sie kürzlich aktualisiert wurden)
    gdrive_id = gdrive_fallback.get("file_id")
    if gdrive_id:
        logger.info(f"All mirrors failed for {description}, falling back to Google Drive...")
        if _download_gdrive(gdrive_id, dest, description, max_retries=5, retry_delay=60):
            if _checksum_ok(dest, expected_sha256, description):
                return True
            dest.unlink(missing_ok=True)
        return False
    
    logger.error(f"No mirrors or GDrive fallback available for {description}")
    return False

"""
    Abstract: Verifies that a .pkl file is valid.
              When full_load=True, loads the entire object with joblib (suitable for small models).
              When full_load=False, does a lightweight check (file size + pickle header)
              to avoid OOM on very large models like UMAP (6+ GB).
    Args:
    - path: File path
    - label: Human-readable label for log messages
    - full_load: Whether to fully load the model into memory for verification
    Returns: bool
"""
def _verify_pkl(path: Path, label: str, full_load: bool = True) -> bool:

    if not path.exists():
        logger.error(f"{label} file does not exist: {path}")
        return False

    file_size = path.stat().st_size
    if file_size < 1024:
        logger.error(f"{label} file is suspiciously small ({file_size} bytes): {path}")
        return False

    if not full_load:
        # Für sehr große Modelle wie UMAP wollen wir nicht das gesamte Objekt laden, da das zu OOM führen könnte. Stattdessen prüfen wir nur die Dateigröße und den Pickle-Header, um eine grundlegende Validierung durchzuführen.
        try:
            with open(path, "rb") as f:
                header = f.read(2)
                if header[0:1] != b'\x80':
                    logger.error(f"{label} does not have a valid pickle header.")
                    return False
            logger.info(f"{label} header verified ({file_size / (1024**3):.2f} GB). Full load deferred to pipeline.")
            return True
        except Exception as e:
            logger.error(f"Failed lightweight verification of {label}: {e}")
            return False

    try:
        obj = joblib.load(path)
        if obj is None:
            logger.error(f"{label} loaded as None – file may be corrupt.")
            return False
        logger.info(f"{label} loaded and verified successfully (type: {type(obj).__name__}).")
        return True
    except Exception as e:
        logger.error(f"Failed to verify {label}: {e}")
        return False




def _get_filename(file_key: str) -> str:
    """Get the filename for a file from mirrors.json config."""
    config = _get_file_config(file_key)
    return config.get("filename", f"{file_key}.bin")


def download_dataset() -> Path | None:
    """
    Abstract: Downloads the full dataset JSONL.
              Tries HTTP mirrors first, then falls back to Google Drive.
    Returns: Path to the downloaded file, or None on failure.
    """
    filename = _get_filename("dataset")
    dest = _data_dir() / filename
    if _download_file("dataset", dest):
        return dest
    return None


def download_models() -> bool:
    """
    Abstract: Downloads PCA and UMAP pre-trained models and verifies they can be loaded.
              Tries HTTP mirrors first, then falls back to Google Drive.
    Returns: bool -> True if both models were downloaded and verified.
    """
    models_dir = _models_dir()
    
    pca_filename = _get_filename("pca_model")
    umap_filename = _get_filename("umap_model")
    
    pca_path = models_dir / pca_filename
    umap_path = models_dir / umap_filename

    pca_ok = _download_file("pca_model", pca_path)
    umap_ok = _download_file("umap_model", umap_path)

    if not pca_ok or not umap_ok:
        return False

    if not _verify_pkl(pca_path, "PCA model"):
        return False
    if not _verify_pkl(umap_path, "UMAP model", full_load=False):
        return False

    return True


def ensure_all_downloaded() -> bool:
    """
    Abstract: Ensures dataset + models are downloaded. Returns False if anything fails.
    """
    dataset_path = download_dataset()
    if dataset_path is None:
        return False
    if not download_models():
        return False
    return True


def ensure_models_exist() -> bool:
    """
    Abstract: Ensures only the ML models (PCA + UMAP) are present (no dataset needed).
              Called on non-first runs where the dataset has already been imported.
    Returns: bool
    """
    return download_models()


def get_dataset_path() -> Path:
    return _data_dir() / _get_filename("dataset")


def get_pca_model_path() -> Path:
    return _models_dir() / _get_filename("pca_model")


def get_umap_model_path() -> Path:
    return _models_dir() / _get_filename("umap_model")


def cleanup_dataset():
    """
    Abstract: Removes the dataset JSONL after successful import to reclaim disk space.
    """
    path = get_dataset_path()
    if path.exists():
        size = path.stat().st_size
        path.unlink()
        logger.info(f"Cleaned up dataset file ({size / (1024**3):.2f} GB freed)")
