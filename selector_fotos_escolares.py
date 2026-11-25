"""Ramirez Films Selector - selector local de fotos escolares para Windows.

La aplicación nunca borra los originales. Analiza una carpeta y crea dos salidas:
  - Seleccionadas: fotos con mejor calidad técnica.
  - Revision: fotos borrosas, pequeñas, duplicadas o que requieren revisión.

Dependencias: Pillow (obligatoria) y OpenCV (opcional, mejora el análisis de
nitidez y rostros). Ver README_SELECTOR.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Y, filedialog, messagebox, ttk
import tkinter as tk

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # pragma: no cover - mensaje visible en la interfaz
    Image = None
    ImageOps = None
    ImageTk = None

try:
    import cv2
    import numpy as np
except ImportError:  # OpenCV es opcional
    cv2 = None
    np = None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
APP_NAME = "PickFlow"
APP_VERSION = "0.7.0"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/ivaanhj123/ramirez-films-selector-escolar/main/version.json"


@dataclass
class PhotoResult:
    source: Path
    width: int = 0
    height: int = 0
    sharpness: float = 0.0
    score: float = 0.0
    reason: str = ""
    selected: bool = False
    duplicate: bool = False
    signature: str = ""


def file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def average_hash(image: "Image.Image") -> str:
    """Hash pequeño para detectar copias visualmente iguales o casi iguales."""
    gray = ImageOps.grayscale(image).resize((16, 16))
    values = list(gray.getdata())
    average = sum(values) / len(values)
    return "".join("1" if value >= average else "0" for value in values)


def photo_signature(image: "Image.Image") -> str:
    """Firma visual del área central, donde normalmente está el alumno."""
    width, height = image.size
    crop = image.crop((int(width * 0.16), int(height * 0.04), int(width * 0.84), int(height * 0.96)))
    return average_hash(crop)


def hamming(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def blur_score(image: "Image.Image") -> float:
    """Devuelve una medida aproximada de nitidez (0-100).

    OpenCV ofrece una medida más precisa. Si no está disponible usamos una
    variación de bordes basada únicamente en Pillow.
    """
    if cv2 is not None and np is not None:
        rgb = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return min(100.0, variance / 8.0)

    # Fallback sin OpenCV: diferencias medias entre píxeles vecinos.
    small = ImageOps.grayscale(image.copy())
    small.thumbnail((900, 900))
    pixels = list(small.getdata())
    width, height = small.size
    if width < 2 or height < 2:
        return 0.0
    horizontal = sum(abs(pixels[i] - pixels[i + 1]) for i in range(len(pixels) - 1))
    vertical = sum(abs(pixels[i] - pixels[i + width]) for i in range(len(pixels) - width))
    scale = max(1, len(pixels) * 2)
    return min(100.0, (horizontal + vertical) / scale * 1.7)


def closed_eyes_warning(image: "Image.Image") -> bool:
    """Marca una posible foto con ojos cerrados cuando OpenCV puede detectarlos.

    Es una señal de revisión, no una decisión definitiva: perfiles, lentes,
    máscaras o rostros muy pequeños pueden producir falsos positivos.
    """
    if cv2 is None or np is None:
        return False
    try:
        gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
        scale = min(1.0, 1200.0 / max(gray.shape))
        if scale < 1.0:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        cascade_root = cv2.data.haarcascades
        face_detector = cv2.CascadeClassifier(os.path.join(cascade_root, "haarcascade_frontalface_default.xml"))
        eye_detector = cv2.CascadeClassifier(os.path.join(cascade_root, "haarcascade_eye_tree_eyeglasses.xml"))
        faces = face_detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(35, 35))
        if len(faces) == 0:
            return False
        for x, y, width, height in faces[:8]:
            upper_face = gray[y : y + int(height * 0.62), x : x + width]
            eyes = eye_detector.detectMultiScale(upper_face, scaleFactor=1.08, minNeighbors=4, minSize=(8, 8))
            if len(eyes) < 1:
                return True
        return False
    except Exception:
        return False


def analyze_photo(path: Path) -> PhotoResult:
    result = PhotoResult(source=path)
    try:
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            result.width, result.height = image.size
            result.sharpness = blur_score(image)
    except Exception as exc:
        result.reason = f"No se pudo leer: {exc}"
        return result

    megapixels = (result.width * result.height) / 1_000_000
    score = min(35.0, megapixels * 4.5)
    score += min(65.0, result.sharpness * 0.65)
    result.score = round(score, 1)

    critical_reasons = []
    warning_reasons = []
    if megapixels < 1.0:
        critical_reasons.append("resolución baja")
    # El umbral anterior era demasiado estricto para retratos escolares.
    if result.sharpness < 7:
        critical_reasons.append("posible desenfoque")
    # Los ojos son una advertencia, no un descarte automático: rostros
    # pequeños, perfiles y lentes provocan falsos positivos con frecuencia.
    if closed_eyes_warning(image):
        warning_reasons.append("revisar ojos")
    result.reason = ", ".join(critical_reasons + warning_reasons)
    result.selected = not critical_reasons
    return result


def select_one_per_series(results: list[PhotoResult], threshold: int = 18) -> None:
    """Conserva una sola ganadora por cada grupo de tomas visualmente parecidas.

    Las fotos se recorren en el orden del nombre de archivo, que coincide con
    el orden de captura de cámaras como Canon IMG_####. Solo se agrupan fotos
    consecutivas para no mezclar alumnos de distintas partes de la sesión.
    """
    if not results:
        return

    groups: list[list[PhotoResult]] = []
    current = [results[0]]
    for result in results[1:]:
        previous = current[-1]
        similar = bool(previous.signature and result.signature and hamming(previous.signature, result.signature) <= threshold)
        if similar:
            current.append(result)
        else:
            groups.append(current)
            current = [result]
    groups.append(current)

    for group in groups:
        if len(group) == 1:
            continue
        winner = max(group, key=lambda item: item.score)
        for item in group:
            item.selected = item is winner
            if item is not winner:
                item.reason = (item.reason + "; misma serie").strip("; ")


class SelectorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} - Selector de Fotos Escolares v{APP_VERSION}")
        self.geometry("900x610")
        self.minsize(760, 500)
        self._icon_image = None
        icon_path = Path(__file__).with_name("pickflow_icon.png")
        if icon_path.exists():
            try:
                self._icon_image = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(True, self._icon_image)
            except tk.TclError:
                self._icon_image = None
        self.folder_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.subfolders_var = tk.BooleanVar(value=True)
        self.one_per_series_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Selecciona la carpeta de fotos para comenzar.")
        self.results: list[PhotoResult] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.preview_images = []
        self.preview_paths: list[Path] = []
        self._build_modern_ui()
        self.after(100, self._poll_events)
        self.after(1400, lambda: self.check_updates(silent=True))

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=16)
        header.pack(fill=X)
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(header, text=f"Selector inteligente de fotos escolares  •  versión {APP_VERSION}", font=("Segoe UI", 11)).pack(anchor="w")

        paths = ttk.LabelFrame(self, text="Carpetas", padding=12)
        paths.pack(fill=X, padx=16, pady=(0, 10))
        ttk.Label(paths, text="Fotos de entrada:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="Elegir...", command=self.choose_folder).grid(row=0, column=2)
        ttk.Label(paths, text="Guardar resultados en:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="Elegir...", command=self.choose_output).grid(row=1, column=2)
        paths.columnconfigure(1, weight=1)
        ttk.Checkbutton(paths, text="Incluir subcarpetas", variable=self.subfolders_var).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Checkbutton(paths, text="Dejar solo una mejor foto por serie", variable=self.one_per_series_var).grid(row=3, column=1, sticky="w", pady=4)

        actions = ttk.Frame(self, padding=(16, 0, 16, 10))
        actions.pack(fill=X)
        self.start_button = ttk.Button(actions, text="Analizar y separar fotos", command=self.start_analysis)
        self.start_button.pack(side=LEFT)
        ttk.Button(actions, text="Buscar actualizaciones", command=self.check_updates).pack(side=RIGHT)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(14, 0))

        summary = ttk.LabelFrame(self, text="Resumen", padding=10)
        summary.pack(fill=X, padx=16, pady=(0, 10))
        self.summary_label = ttk.Label(summary, text="Todavía no hay un análisis.")
        self.summary_label.pack(anchor="w")
        ttk.Label(summary, textvariable=self.status_var, foreground="#555555").pack(anchor="w", pady=(4, 0))

        table_frame = ttk.Frame(self, padding=(16, 0, 16, 16))
        table_frame.pack(fill=BOTH, expand=True)
        columns = ("archivo", "tamaño", "nitidez", "resultado", "motivo")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"archivo": "Archivo", "tamaño": "Tamaño", "nitidez": "Nitidez", "resultado": "Resultado", "motivo": "Motivo"}
        widths = {"archivo": 350, "tamaño": 105, "nitidez": 90, "resultado": 110, "motivo": 220}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)

    def _build_modern_ui(self) -> None:
        """Interfaz PickFlow con carga y vista previa de miniaturas."""
        bg, panel, text, muted = "#111936", "#192650", "#f3f5ff", "#aab6dc"
        self.configure(bg=bg)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Pick.TButton", background="#f05092", foreground="white", borderwidth=0, padding=(14, 8))
        style.map("Pick.TButton", background=[("active", "#ff70a8")])
        style.configure("Dark.TButton", background="#263869", foreground=text, borderwidth=0, padding=(10, 7))
        style.map("Dark.TButton", background=[("active", "#38518b")])
        style.configure("Dark.TCheckbutton", background=bg, foreground=text)

        topbar = tk.Frame(self, bg="#0b142e", height=54)
        topbar.pack(fill=X)
        topbar.pack_propagate(False)
        icon_path = Path(__file__).with_name("pickflow_icon.png")
        if icon_path.exists() and Image is not None and ImageTk is not None:
            try:
                icon = Image.open(icon_path).convert("RGBA")
                icon.thumbnail((30, 30), Image.Resampling.LANCZOS)
                self._header_icon = ImageTk.PhotoImage(icon)
                tk.Label(topbar, image=self._header_icon, bg="#0b142e").pack(side=LEFT, padx=(18, 8))
            except Exception:
                pass
        tk.Label(topbar, text=APP_NAME, bg="#0b142e", fg=text, font=("Segoe UI", 14, "bold")).pack(side=LEFT)
        tk.Label(topbar, text="Selector Visual", bg="#213566", fg="#c4d0ff", font=("Segoe UI", 8, "bold"), padx=8, pady=3).pack(side=LEFT, padx=10)
        tk.Label(topbar, text=f"v{APP_VERSION}", bg="#0b142e", fg=muted, font=("Segoe UI", 9)).pack(side=RIGHT, padx=18)

        body = tk.Frame(self, bg=bg)
        body.pack(fill=BOTH, expand=True)
        upload = tk.Frame(body, bg=panel, highlightbackground="#d64b86", highlightthickness=1)
        upload.pack(padx=24, pady=(22, 12), ipadx=30, ipady=18)
        self.upload_card = upload
        tk.Label(upload, text="▧", bg=panel, fg="#ffb84d", font=("Segoe UI Symbol", 28)).pack()
        tk.Label(upload, text="Cargar imagenes", bg=panel, fg=text, font=("Segoe UI", 13, "bold")).pack()
        tk.Label(upload, text="Haz clic para elegir una carpeta de fotos", bg=panel, fg="#c3cbed", font=("Segoe UI", 9)).pack(pady=(3, 7))
        tk.Label(upload, text="● JPG   PNG   WEBP   TIFF", bg=panel, fg="#78e6a2", font=("Segoe UI", 8)).pack()
        for widget in upload.winfo_children():
            widget.bind("<Button-1>", lambda _event: self.choose_folder())

        paths = tk.Frame(body, bg=bg)
        paths.pack(fill=X, padx=24, pady=(0, 8))
        tk.Label(paths, text="Carpeta de fotos", bg=bg, fg=muted, font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.folder_entry = tk.Entry(paths, textvariable=self.folder_var, bg="#1a2549", fg=text, insertbackground=text, relief="flat", bd=0)
        self.folder_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), ipady=7)
        ttk.Button(paths, text="Elegir...", style="Dark.TButton", command=self.choose_folder).grid(row=1, column=1)
        tk.Label(paths, text="Guardar resultados en", bg=bg, fg=muted, font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w", padx=(18, 0))
        tk.Entry(paths, textvariable=self.output_var, bg="#1a2549", fg=text, insertbackground=text, relief="flat", bd=0).grid(row=1, column=2, sticky="ew", padx=(18, 8), ipady=7)
        ttk.Button(paths, text="Elegir...", style="Dark.TButton", command=self.choose_output).grid(row=1, column=3)
        paths.columnconfigure(0, weight=2)
        paths.columnconfigure(2, weight=2)

        options = tk.Frame(body, bg=bg)
        options.pack(fill=X, padx=24, pady=(0, 8))
        ttk.Checkbutton(options, text="Incluir subcarpetas", variable=self.subfolders_var, style="Dark.TCheckbutton").pack(side=LEFT)
        ttk.Checkbutton(options, text="Una mejor foto por serie", variable=self.one_per_series_var, style="Dark.TCheckbutton").pack(side=LEFT, padx=20)
        ttk.Button(options, text="Buscar actualizaciones", style="Dark.TButton", command=self.check_updates).pack(side=RIGHT)

        actions = tk.Frame(body, bg=bg)
        actions.pack(fill=X, padx=24, pady=(0, 8))
        self.start_button = ttk.Button(actions, text="Analizar y separar fotos", style="Pick.TButton", command=self.start_analysis)
        self.start_button.pack(side=LEFT)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(14, 0))

        summary = tk.Frame(body, bg=panel)
        summary.pack(fill=X, padx=24, pady=(0, 10))
        self.summary_label = tk.Label(summary, text="Todavia no hay un analisis.", bg=panel, fg=text, font=("Segoe UI", 10, "bold"))
        self.summary_label.pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(summary, textvariable=self.status_var, bg=panel, fg=muted, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(2, 8))

        preview_box = tk.Frame(body, bg=panel)
        preview_box.pack(fill=BOTH, expand=True, padx=24, pady=(0, 10))
        preview_header = tk.Frame(preview_box, bg=panel)
        preview_header.pack(fill=X)
        tk.Label(preview_header, text="Vista previa", bg=panel, fg=text, font=("Segoe UI", 11, "bold")).pack(side=LEFT, padx=12, pady=8)
        self.preview_count_label = tk.Label(preview_header, text="Elige una carpeta para ver miniaturas", bg=panel, fg=muted, font=("Segoe UI", 9))
        self.preview_count_label.pack(side=RIGHT, padx=12)
        canvas_frame = tk.Frame(preview_box, bg=panel)
        canvas_frame.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))
        self.preview_canvas = tk.Canvas(canvas_frame, bg=panel, highlightthickness=0)
        preview_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.preview_canvas.yview)
        self.preview_inner = tk.Frame(self.preview_canvas, bg=panel)
        self.preview_window = self.preview_canvas.create_window((0, 0), window=self.preview_inner, anchor="nw")
        self.preview_inner.bind("<Configure>", lambda _event: self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all")))
        self.preview_canvas.bind("<Configure>", lambda event: self.preview_canvas.itemconfigure(self.preview_window, width=event.width))
        self.preview_canvas.configure(yscrollcommand=preview_scroll.set)
        self.preview_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        preview_scroll.pack(side=RIGHT, fill=Y)

        table_frame = ttk.Frame(body, padding=(0, 0, 0, 16))
        table_frame.pack(fill=BOTH, expand=True, padx=24)
        columns = ("archivo", "tamaño", "nitidez", "resultado", "motivo")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"archivo": "Archivo", "tamaño": "Tamaño", "nitidez": "Nitidez", "resultado": "Resultado", "motivo": "Motivo"}
        widths = {"archivo": 350, "tamaño": 105, "nitidez": 90, "resultado": 110, "motivo": 220}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Elige la carpeta con fotos escolares")
        if selected:
            self.folder_var.set(selected)
            if not self.output_var.get():
                self.output_var.set(str(Path(selected) / "Resultado_Selector"))
            self._start_preview(Path(selected))

    def _start_preview(self, folder: Path) -> None:
        if Image is None:
            return
        for widget in self.preview_inner.winfo_children():
            widget.destroy()
        self.preview_images.clear()
        self.preview_paths.clear()
        self.preview_count_label.configure(text="Cargando miniaturas...")
        threading.Thread(target=self._preview_worker, args=(folder,), daemon=True).start()

    def _preview_worker(self, folder: Path) -> None:
        try:
            iterator = folder.rglob("*") if self.subfolders_var.get() else folder.glob("*")
            files = sorted((p for p in iterator if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=lambda p: str(p).lower())
            shown = 0
            for path in files[:120]:
                try:
                    with Image.open(path) as original:
                        image = ImageOps.exif_transpose(original).convert("RGB")
                        image.thumbnail((118, 84), Image.Resampling.LANCZOS)
                        self.events.put(("preview", (path, image.copy())))
                        shown += 1
                except Exception:
                    continue
            self.events.put(("preview_done", (shown, len(files))))
        except Exception as exc:
            self.events.put(("preview_error", str(exc)))

    def _add_thumbnail(self, path: Path, image: "Image.Image") -> None:
        if ImageTk is None:
            return
        photo = ImageTk.PhotoImage(image)
        self.preview_images.append(photo)
        self.preview_paths.append(path)
        index = len(self.preview_images) - 1
        card = tk.Frame(self.preview_inner, bg="#253663", padx=4, pady=4)
        card.grid(row=index // 6, column=index % 6, padx=5, pady=5, sticky="n")
        label = tk.Label(card, image=photo, bg="#253663")
        label.pack()
        tk.Label(card, text=path.name[:20], bg="#253663", fg="#e8ecff", font=("Segoe UI", 7), width=18).pack(pady=(3, 0))

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Elige dónde guardar el resultado")
        if selected:
            self.output_var.set(selected)

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        numbers = []
        for part in str(value).strip().lstrip("v").split("."):
            try:
                numbers.append(int(part))
            except ValueError:
                numbers.append(0)
        return tuple(numbers or [0])

    def check_updates(self, silent: bool = False) -> None:
        """Comprueba y aplica una actualización publicada en GitHub."""
        try:
            request = urllib.request.Request(
                UPDATE_MANIFEST_URL,
                headers={"User-Agent": "RamirezFilmsSelector/" + APP_VERSION},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            latest = str(manifest.get("version", APP_VERSION))
            if self._version_tuple(latest) <= self._version_tuple(APP_VERSION):
                if not silent:
                    messagebox.showinfo("Actualizaciones", f"Ya tienes la versión más reciente ({APP_VERSION}).")
                return
            download_url = manifest.get("download_url")
            notes = manifest.get("notes", "Hay una nueva versión disponible.")
            if not download_url:
                raise ValueError("La actualización no tiene una dirección de descarga.")
            if messagebox.askyesno("Nueva actualización", f"Versión {latest}\n\n{notes}\n\n¿Descargar e instalar ahora?"):
                self._download_and_apply_update(str(download_url), latest)
        except Exception as exc:
            if not silent:
                messagebox.showwarning("Actualizaciones", f"No se pudo comprobar ahora.\n\n{exc}")

    def _download_and_apply_update(self, download_url: str, latest: str) -> None:
        """Descarga un ZIP de GitHub, reemplaza los archivos y reinicia."""
        app_dir = Path(__file__).resolve().parent
        zip_path = app_dir / ".selector_update.zip"
        extract_dir = app_dir / ".selector_update_extracted"
        try:
            self.status_var.set(f"Descargando actualización {latest}...")
            request = urllib.request.Request(download_url, headers={"User-Agent": "RamirezFilmsSelector/" + APP_VERSION})
            with urllib.request.urlopen(request, timeout=60) as response, zip_path.open("wb") as target:
                shutil.copyfileobj(response, target)
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extract_dir)
            source_dir = extract_dir
            nested = list(extract_dir.iterdir())
            if len(nested) == 1 and nested[0].is_dir():
                source_dir = nested[0]
            for item in source_dir.iterdir():
                if item.name.startswith("."):
                    continue
                destination = app_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
            zip_path.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            messagebox.showinfo("Actualización instalada", "La aplicación se actualizará y se abrirá de nuevo.")
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(app_dir))
            self.destroy()
        except Exception:
            zip_path.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise

    def start_analysis(self) -> None:
        if self.running:
            return
        if Image is None:
            messagebox.showerror("Falta Pillow", "Instala Pillow con: python -m pip install Pillow")
            return
        folder = Path(self.folder_var.get()).expanduser()
        if not folder.is_dir():
            messagebox.showwarning("Carpeta no válida", "Selecciona una carpeta de fotos válida.")
            return
        output = Path(self.output_var.get() or (folder / "Resultado_Selector"))
        if output.resolve() == folder.resolve():
            messagebox.showwarning("Carpeta de salida", "El resultado debe estar en otra carpeta para proteger los originales.")
            return
        self.output_var.set(str(output))
        self.running = True
        self.start_button.configure(state="disabled")
        self.progress.configure(value=0, maximum=1)
        self.status_var.set("Buscando fotos...")
        threading.Thread(target=self._worker, args=(folder, output, self.one_per_series_var.get()), daemon=True).start()

    def _worker(self, folder: Path, output: Path, one_per_series: bool) -> None:
        try:
            iterator = folder.rglob("*") if self.subfolders_var.get() else folder.glob("*")
            files = sorted((p for p in iterator if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and output not in p.parents), key=lambda p: str(p).lower())
            self.events.put(("start", len(files)))
            results: list[PhotoResult] = []
            exact_hashes: dict[str, Path] = {}
            visual_hashes: list[tuple[str, Path]] = []
            for index, path in enumerate(files, 1):
                result = analyze_photo(path)
                try:
                    raw_hash = file_hash(path)
                    if raw_hash in exact_hashes:
                        result.duplicate = True
                        result.selected = False
                        result.reason = "duplicada exacta"
                    else:
                        exact_hashes[raw_hash] = path
                        with Image.open(path) as img:
                            visual = photo_signature(ImageOps.exif_transpose(img).convert("RGB"))
                        result.signature = visual
                        if any(hamming(visual, old_hash) <= 4 for old_hash, _ in visual_hashes):
                            # No se descarta por similitud; solo se marca para revisión.
                            result.reason = (result.reason + "; posible repetida").strip("; ")
                        visual_hashes.append((visual, path))
                except Exception:
                    pass
                results.append(result)
                self.events.put(("progress", (index, len(files), result)))

            if one_per_series:
                select_one_per_series(results)

            selected_dir = output / "Seleccionadas"
            review_dir = output / "Revision"
            selected_dir.mkdir(parents=True, exist_ok=True)
            review_dir.mkdir(parents=True, exist_ok=True)
            for result in results:
                destination_root = selected_dir if result.selected else review_dir
                relative = result.source.relative_to(folder) if folder in result.source.parents else Path(result.source.name)
                destination = destination_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result.source, destination)
            self.events.put(("done", results))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "start":
                    self.progress.configure(maximum=max(1, int(payload)))
                    self.status_var.set(f"Analizando {payload} fotos...")
                elif event == "progress":
                    index, total, result = payload
                    self.progress.configure(value=index)
                    self.status_var.set(f"Analizando {index} de {total}: {result.source.name}")
                elif event == "preview":
                    path, image = payload
                    self._add_thumbnail(path, image)
                elif event == "preview_done":
                    shown, total = payload
                    suffix = "" if total <= shown else " (se muestran las primeras 120)"
                    self.preview_count_label.configure(text=f"{total} imagenes encontradas{suffix}")
                elif event == "preview_error":
                    self.preview_count_label.configure(text="No se pudieron cargar las miniaturas")
                elif event == "done":
                    self.results = payload
                    self._show_results()
                    selected = sum(item.selected for item in self.results)
                    self.summary_label.configure(text=f"Listo: {len(self.results)} fotos analizadas | {selected} seleccionadas | {len(self.results) - selected} en revisión")
                    self.status_var.set("Terminado. Revisa las carpetas Seleccionadas y Revision.")
                    self.running = False
                    self.start_button.configure(state="normal")
                elif event == "error":
                    messagebox.showerror("Error", payload)
                    self.status_var.set("Ocurrió un error durante el análisis.")
                    self.running = False
                    self.start_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _show_results(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for result in self.results:
            outcome = "Seleccionada" if result.selected else "Revisión"
            self.table.insert("", END, values=(result.source.name, f"{result.width} × {result.height}", f"{result.sharpness:.1f}", outcome, result.reason or "calidad aceptable"))


if __name__ == "__main__":
    app = SelectorApp()
    app.mainloop()
