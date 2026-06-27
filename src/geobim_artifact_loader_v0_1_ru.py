"""Переносимый загрузчик артефактов быстрого запуска GeoBIM Mentor.

Модуль проверяет и открывает комплект быстрого запуска, созданный ноутбуком GeoBIM Mentor
v0.1. Он намеренно содержит только повторно используемый поисковый слой:
проверку артефакта, подключение Chroma, семантический поиск и доступ к
включённым служебным файлам. Ключи доступа, веса моделей и полный конвейер ответа
не встраиваются в этот файл Python; приватные PDF-источники также отсутствуют.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA = "geobim_mentor_runtime_artifact"
SUPPORTED_FORMAT_MAJOR = 1
MANIFEST_NAME = "manifest.json"
__version__ = "0.1.0"

# Запас по ограничениям достаточно велик для практического снимка Chroma, но
# остаётся конечным, чтобы повреждённый или вредоносный архив не мог распаковаться без ограничений.
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 20 * 1024**3  # 20 GiB


class GeoBIMArtifactError(RuntimeError):
    """Исключение для отсутствующего или несовместимого переносимого артефакта GeoBIM."""


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Возвращает SHA-256, не загружая весь файл в память."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Совместимый с разными версиями Python аналог `Path.is_relative_to`."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_zip_symlink(member: zipfile.ZipInfo) -> bool:
    """Возвращает `True`, если элемент ZIP в формате Unix является символической ссылкой."""
    unix_mode = member.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def safe_extract_zip(
    zip_path: str | Path,
    target_dir: str | Path,
    *,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> Path:
    """Распаковывает ZIP после проверки целостности, путей и ресурсных ограничений.

    Целевой каталог должен использоваться только для этой распаковки. Функция
    отклоняет абсолютные пути и выход за каталог, дубли нормализованных путей, зашифрованные
    элементы, символические ссылки Unix, чрезмерное число элементов и чрезмерный общий
    распакованный размер до записи любого элемента архива.
    """
    zip_path = Path(zip_path).expanduser().resolve()
    target_dir = Path(target_dir).expanduser().resolve()

    if not zip_path.is_file() or not zipfile.is_zipfile(zip_path):
        raise GeoBIMArtifactError(f"Artifact is not a readable ZIP: {zip_path}")

    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > int(max_members):
            raise GeoBIMArtifactError(
                f"Artifact contains too many members: {len(members)} > {max_members}."
            )

        total_uncompressed = sum(max(0, int(member.file_size)) for member in members)
        if total_uncompressed > int(max_uncompressed_bytes):
            raise GeoBIMArtifactError(
                "Artifact uncompressed size exceeds the configured limit: "
                f"{total_uncompressed} > {max_uncompressed_bytes}."
            )

        bad_member = archive.testzip()
        if bad_member:
            raise GeoBIMArtifactError(f"Corrupted ZIP member: {bad_member}")

        normalized_destinations: set[Path] = set()
        for member in members:
            if member.flag_bits & 0x1:
                raise GeoBIMArtifactError(
                    f"Encrypted ZIP members are not supported: {member.filename}"
                )
            if _is_zip_symlink(member):
                raise GeoBIMArtifactError(
                    f"Symbolic links are not allowed in artifacts: {member.filename}"
                )

            destination = (target_dir / member.filename).resolve()
            if not _is_relative_to(destination, target_dir):
                raise GeoBIMArtifactError(
                    f"Unsafe path inside artifact: {member.filename}"
                )
            if destination in normalized_destinations:
                raise GeoBIMArtifactError(
                    f"Duplicate normalized path inside artifact: {member.filename}"
                )
            normalized_destinations.add(destination)

        archive.extractall(target_dir)

    return target_dir


def _format_major(version: Any) -> int:
    """Извлекает старший номер версии формата."""
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise GeoBIMArtifactError(
            f"Invalid artifact format version: {version!r}"
        ) from exc


def read_manifest(artifact_root: str | Path) -> dict[str, Any]:
    """Читает `manifest.json` и выполняет структурную проверку."""
    artifact_root = Path(artifact_root).expanduser().resolve()
    manifest_path = artifact_root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise GeoBIMArtifactError(f"Missing {MANIFEST_NAME}: {artifact_root}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GeoBIMArtifactError(f"Invalid manifest JSON: {manifest_path}") from exc

    if not isinstance(manifest, dict):
        raise GeoBIMArtifactError("Artifact manifest must be a JSON object.")
    if manifest.get("schema") != ARTIFACT_SCHEMA:
        raise GeoBIMArtifactError(
            f"Unsupported artifact schema: {manifest.get('schema')!r}"
        )

    distribution = manifest.get("distribution")
    if distribution not in {"public", "local_only"}:
        raise GeoBIMArtifactError(
            f"Unsupported artifact distribution: {distribution!r}"
        )
    if manifest.get("contains_private_derivatives") is True and distribution != "local_only":
        raise GeoBIMArtifactError(
            "Artifact contains private derivatives but is not marked local_only."
        )
    if manifest.get("secrets_included") is True:
        raise GeoBIMArtifactError("Artifacts containing secrets are not accepted.")

    if _format_major(manifest.get("format_version")) != SUPPORTED_FORMAT_MAJOR:
        raise GeoBIMArtifactError(
            "Incompatible artifact format: "
            f"{manifest.get('format_version')!r}; expected major "
            f"{SUPPORTED_FORMAT_MAJOR}."
        )

    index_info = manifest.get("index") or {}
    required_index_fields = {
        "engine",
        "collection_name",
        "relative_path",
        "embedding_model",
        "count",
    }
    missing = sorted(required_index_fields - set(index_info))
    if missing:
        raise GeoBIMArtifactError(
            f"Manifest index section is missing fields: {', '.join(missing)}"
        )
    if index_info.get("engine") != "chroma":
        raise GeoBIMArtifactError(
            f"Unsupported index engine: {index_info.get('engine')!r}"
        )
    try:
        if int(index_info["count"]) < 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise GeoBIMArtifactError("Manifest index.count must be a non-negative integer.") from exc

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise GeoBIMArtifactError("Manifest files section must be a non-empty object.")
    return manifest


def _actual_payload_files(artifact_root: Path) -> set[str]:
    """Возвращает все обычные файлы содержимого, кроме самого манифеста."""
    return {
        path.relative_to(artifact_root).as_posix()
        for path in artifact_root.rglob("*")
        if path.is_file() and path.relative_to(artifact_root).as_posix() != MANIFEST_NAME
    }


def validate_artifact(
    artifact_root: str | Path,
    *,
    verify_hashes: bool = True,
    expected_compatibility_id: str | None = None,
    allow_local_only: bool = False,
) -> dict[str, Any]:
    """Проверяет схему, правила распространения, пути, размеры файлов и SHA-256.

    ``allow_local_only`` is намеренно ``False`` by значение по умолчанию.  A bundle whose
    манифест says that it contains derivatives of private PDFs must therefore be
    accepted explicitly by the векторизация application.
    """
    artifact_root = Path(artifact_root).expanduser().resolve()
    manifest = read_manifest(artifact_root)
    index_info = manifest["index"]

    if manifest.get("distribution") == "local_only" and not allow_local_only:
        raise GeoBIMArtifactError(
            "Artifact is marked local_only because it may contain derivatives of "
            "private documents. Pass allow_local_only=True only in an authorized "
            "local environment."
        )

    if expected_compatibility_id is not None:
        actual = str(index_info.get("compatibility_id", ""))
        if actual != str(expected_compatibility_id):
            raise GeoBIMArtifactError(
                "Artifact compatibility_id mismatch: "
                f"expected {expected_compatibility_id!r}, got {actual!r}."
            )

    chroma_path = (artifact_root / index_info["relative_path"]).resolve()
    if not _is_relative_to(chroma_path, artifact_root) or not chroma_path.is_dir():
        raise GeoBIMArtifactError(f"Missing Chroma directory: {chroma_path}")

    files: dict[str, Any] = manifest["files"]
    listed_files = set(files)
    actual_files = _actual_payload_files(artifact_root)
    untracked = sorted(actual_files - listed_files)
    if untracked:
        raise GeoBIMArtifactError(
            "Artifact contains files not covered by the manifest: "
            + ", ".join(untracked[:10])
        )

    for relative_name, metadata in files.items():
        path = (artifact_root / relative_name).resolve()
        if not _is_relative_to(path, artifact_root):
            raise GeoBIMArtifactError(f"Unsafe manifest path: {relative_name}")
        if not path.is_file():
            raise GeoBIMArtifactError(f"Artifact file is missing: {relative_name}")

        try:
            expected_size = int((metadata or {}).get("size", -1))
        except (TypeError, ValueError) as exc:
            raise GeoBIMArtifactError(
                f"Invalid file size in manifest: {relative_name}"
            ) from exc
        if expected_size < 0 or path.stat().st_size != expected_size:
            raise GeoBIMArtifactError(f"File size mismatch: {relative_name}")

        if verify_hashes:
            expected_hash = str((metadata or {}).get("sha256", "")).lower()
            if len(expected_hash) != 64 or sha256_file(path) != expected_hash:
                raise GeoBIMArtifactError(f"SHA-256 mismatch: {relative_name}")

    return manifest


def _cache_key(bundle_path: Path) -> str:
    """Создаёт ключ адресуемого по содержимому кэша из самого ZIP-артефакта.

    Reading the ZIP once is inexpensive compared with модель loading and prevents
    stale-cache reuse when a file is replaced while retaining its name, size,
    and modification timestamp.
    """
    return sha256_file(bundle_path)[:24]


def materialize_artifact(
    bundle_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    verify_hashes: bool = True,
    force_extract: bool = False,
    expected_compatibility_id: str | None = None,
    allow_local_only: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Распаковывает и проверяет комплект с атомарной заменой каталога кэша."""
    bundle_path = Path(bundle_path).expanduser().resolve()
    if not bundle_path.is_file():
        raise GeoBIMArtifactError(f"Artifact bundle not found: {bundle_path}")

    base_cache = Path(
        cache_dir
        or os.environ.get("GEOBIM_ARTIFACT_CACHE", "~/.cache/geobim_mentor")
    ).expanduser().resolve()
    artifact_root = base_cache / f"{bundle_path.stem}_{_cache_key(bundle_path)}"

    validation_kwargs = {
        "verify_hashes": verify_hashes,
        "expected_compatibility_id": expected_compatibility_id,
        "allow_local_only": allow_local_only,
    }
    if artifact_root.exists() and not force_extract:
        try:
            return artifact_root, validate_artifact(
                artifact_root,
                **validation_kwargs,
            )
        except GeoBIMArtifactError:
            shutil.rmtree(artifact_root, ignore_errors=True)

    base_cache.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="geobim_artifact_", dir=base_cache))
    try:
        safe_extract_zip(bundle_path, temp_root)
        manifest = validate_artifact(temp_root, **validation_kwargs)
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
        temp_root.replace(artifact_root)
        return artifact_root, manifest
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


@dataclass(slots=True)
class LoadedGeoBIMArtifact:
    """Проверенный артефакт и лениво открываемые объекты Chroma и LlamaIndex."""

    root: Path
    manifest: dict[str, Any]
    chroma_path: Path
    client: Any = None
    collection: Any = None
    index: Any = None
    embed_model: Any = None

    @property
    def distribution(self) -> str:
        """Возвращает режим распространения из манифеста (`public` или `local_only`)."""
        return str(self.manifest.get("distribution", "unknown"))

    def support_path(self, key: str) -> Path:
        """Возвращает проверенный путь к служебному файлу, объявленному в манифесте."""
        support_files = self.manifest.get("support_files") or {}
        if key not in support_files:
            raise KeyError(f"Unknown support file key: {key}")
        path = (self.root / support_files[key]).resolve()
        if not _is_relative_to(path, self.root) or not path.is_file():
            raise GeoBIMArtifactError(f"Invalid support file path for key: {key}")
        return path

    def read_support_text(self, key: str, *, encoding: str = "utf-8") -> str:
        """Читает объявленный в манифесте служебный файл как текст."""
        return self.support_path(key).read_text(encoding=encoding)

    def read_support_json(self, key: str) -> Any:
        """Читает объявленный в манифесте служебный файл JSON."""
        try:
            return json.loads(self.read_support_text(key))
        except json.JSONDecodeError as exc:
            raise GeoBIMArtifactError(
                f"Support file is not valid JSON: {key}"
            ) from exc

    def extract_support_archive(
        self,
        key: str,
        target_dir: str | Path,
        *,
        clean: bool = False,
    ) -> Path:
        """Безопасно распаковывает объявленный в манифесте служебный архив ZIP."""
        archive_path = self.support_path(key)
        if archive_path.suffix.lower() != ".zip":
            raise GeoBIMArtifactError(f"Support file is not a ZIP archive: {key}")
        target = Path(target_dir).expanduser().resolve()
        if clean and target.exists():
            shutil.rmtree(target)
        return safe_extract_zip(archive_path, target)

    def open_index(
        self,
        *,
        device: str | None = None,
        embed_batch_size: int | None = None,
        hf_token: str | None = None,
    ) -> "LoadedGeoBIMArtifact":
        """Открывает сохранённую коллекцию Chroma без повторного вычисления векторных представлений."""
        try:
            import chromadb
            from llama_index.core import VectorStoreIndex
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
            from llama_index.vector_stores.chroma import ChromaVectorStore
        except ImportError as exc:
            raise GeoBIMArtifactError(
                "Runtime dependencies are missing. Install requirements_runtime.txt "
                "from the artifact bundle."
            ) from exc

        index_info = self.manifest["index"]
        kwargs: dict[str, Any] = {
            "model_name": index_info["embedding_model"],
        }
        if device:
            kwargs["device"] = device
        if embed_batch_size is not None:
            if int(embed_batch_size) <= 0:
                raise ValueError("embed_batch_size must be positive")
            kwargs["embed_batch_size"] = int(embed_batch_size)

        effective_hf_token = hf_token or os.environ.get("HF_TOKEN") or None
        if effective_hf_token:
            kwargs["model_kwargs"] = {"token": effective_hf_token}

        try:
            self.embed_model = HuggingFaceEmbedding(**kwargs)
        except TypeError:
            # Совместимость с интеграциями, которые не принимают `model_kwargs`.
            kwargs.pop("model_kwargs", None)
            self.embed_model = HuggingFaceEmbedding(**kwargs)

        self.client = chromadb.PersistentClient(path=str(self.chroma_path))
        try:
            self.collection = self.client.get_collection(
                index_info["collection_name"]
            )
        except Exception as exc:
            raise GeoBIMArtifactError(
                "The Chroma collection declared by the manifest is absent: "
                f"{index_info['collection_name']}"
            ) from exc

        actual_count = int(self.collection.count())
        expected_count = int(index_info["count"])
        if actual_count != expected_count:
            raise GeoBIMArtifactError(
                f"Chroma record count mismatch: expected {expected_count}, "
                f"got {actual_count}."
            )

        vector_store = ChromaVectorStore(chroma_collection=self.collection)
        self.index = VectorStoreIndex.from_vector_store(
            vector_store,
            embed_model=self.embed_model,
        )
        return self

    def as_retriever(self, *, top_k: int = 5) -> Any:
        """Возвращает объект поиска LlamaIndex, настроенный для этого артефакта."""
        if self.index is None:
            raise GeoBIMArtifactError("Call open_index() before as_retriever().")
        if int(top_k) <= 0:
            raise ValueError("top_k must be positive")
        return self.index.as_retriever(similarity_top_k=int(top_k))

    def retrieve(self, query: str, *, top_k: int = 5) -> list[Any]:
        """Выполняет семантический поиск и возвращает объекты `NodeWithScore` LlamaIndex."""
        if not str(query).strip():
            raise ValueError("query must not be empty")
        return self.as_retriever(top_k=top_k).retrieve(str(query).strip())

    def retrieve_dicts(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """Возвращает результаты поиска в виде словарей, сериализуемых в JSON."""
        rows: list[dict[str, Any]] = []
        for item in self.retrieve(query, top_k=top_k):
            node = getattr(item, "node", item)
            get_content = getattr(node, "get_content", None)
            text = get_content() if callable(get_content) else str(getattr(node, "text", ""))
            rows.append(
                {
                    "score": getattr(item, "score", None),
                    "text": text,
                    "metadata": dict(getattr(node, "metadata", {}) or {}),
                    "node_id": getattr(node, "node_id", None),
                }
            )
        return rows


def load_geobim_artifact(
    bundle_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    verify_hashes: bool = True,
    force_extract: bool = False,
    open_index: bool = True,
    device: str | None = None,
    embed_batch_size: int | None = None,
    hf_token: str | None = None,
    expected_compatibility_id: str | None = None,
    allow_local_only: bool = False,
) -> LoadedGeoBIMArtifact:
    """Проверяет комплект и при необходимости открывает сохранённый поисковый индекс."""
    root, manifest = materialize_artifact(
        bundle_path,
        cache_dir=cache_dir,
        verify_hashes=verify_hashes,
        force_extract=force_extract,
        expected_compatibility_id=expected_compatibility_id,
        allow_local_only=allow_local_only,
    )
    chroma_path = (root / manifest["index"]["relative_path"]).resolve()
    loaded = LoadedGeoBIMArtifact(
        root=root,
        manifest=manifest,
        chroma_path=chroma_path,
    )
    if open_index:
        loaded.open_index(
            device=device,
            embed_batch_size=embed_batch_size,
            hf_token=hf_token,
        )
    return loaded


__all__ = [
    "ARTIFACT_SCHEMA",
    "__version__",
    "DEFAULT_MAX_ARCHIVE_MEMBERS",
    "DEFAULT_MAX_UNCOMPRESSED_BYTES",
    "GeoBIMArtifactError",
    "LoadedGeoBIMArtifact",
    "load_geobim_artifact",
    "materialize_artifact",
    "read_manifest",
    "safe_extract_zip",
    "sha256_file",
    "validate_artifact",
]
