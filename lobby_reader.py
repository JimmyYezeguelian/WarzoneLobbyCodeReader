"""Lecteur OCR d'une zone choisie sur l'ensemble des ecrans Windows."""

import ctypes
import re
import threading
import time
import warnings
from pathlib import Path

import cv2
import easyocr
import mss
import numpy as np
import pyperclip
import tkinter as tk
from PIL import Image, ImageTk


# EasyOCR active cette optimisation PyTorch meme en mode CPU. Elle est sans
# consequence ici et ne doit pas masquer les autres avertissements.
warnings.filterwarnings(
    "ignore",
    message=r"'pin_memory' argument is set as true but no accelerator is found.*",
    category=UserWarning,
    module=r"torch\.utils\.data\.dataloader",
)


# Evite un decalage entre Tk et mss lorsque les ecrans ont des echelles DPI differentes.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# Cette variable est lue par le thread OCR et modifiee par l'interface.
# Le verrou evite qu'une capture utilise une zone partiellement mise a jour.
CONFIG_LOCK = threading.Lock()

# ROI (Region Of Interest) : rectangle a capturer en coordonnees globales du
# bureau Windows. Les coordonnees peuvent donc etre negatives sur un ecran a gauche.
ROI = {"left": 0, "top": 0, "width": 300, "height": 70}

# Frequence de lecture OCR et frequence de mise a jour visuelle de l'apercu.
DELAY = 0.3
PREVIEW_DELAY = 1.0
PREVIEW_SIZE = (360, 140)
# L'icone est conservee a cote du script, ce qui fonctionne aussi apres deplacement du dossier.
ICON_PATH = Path(__file__).with_name("lobby_reader_icon.ico")


# L'initialisation du modele est longue : elle est faite une seule fois au demarrage.
print("Chargement OCR...")
reader = easyocr.Reader(["en"], gpu=False)
print("OCR pret")


def read_number(image):
    """Retourne uniquement les chiffres lus dans l'image BGR."""
    # Les chiffres agrandis et en niveaux de gris sont plus faciles a reconnaitre.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    text = "".join(reader.readtext(gray, detail=0, allowlist="0123456789"))
    return re.sub(r"\D", "", text)


def select_roi(ui):
    """Affiche un voile sur chaque moniteur et selectionne une ROI globale."""
    # La fenetre principale est cachee pour ne pas etre selectionnee par erreur.
    ui.root.withdraw()
    with mss.MSS() as sct:
        monitors = sct.monitors[1:]

    # Un overlay transparent par ecran permet de capter la souris partout.
    overlays = []
    # Etat temporaire du glisser-deposer en cours.
    state = {"start_x": None, "start_y": None, "canvas": None, "rect": None}

    def close_selector(_event=None):
        """Ferme tous les voiles et remet la fenetre principale au premier plan."""
        for overlay in overlays:
            if overlay.winfo_exists():
                overlay.destroy()
        ui.root.deiconify()
        ui.root.lift()

    def start_selection(event, _monitor, canvas):
        # event.x_root / y_root sont des coordonnees de bureau, pas locales au Canvas.
        state.update(
            start_x=event.x_root,
            start_y=event.y_root,
            canvas=canvas,
            rect=None,
        )

    def draw_selection(event):
        canvas = state["canvas"]
        if canvas is None:
            return
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        # Canvas attend des coordonnees locales pour dessiner le rectangle rouge.
        x1 = state["start_x"] - canvas.winfo_rootx()
        y1 = state["start_y"] - canvas.winfo_rooty()
        x2 = event.x_root - canvas.winfo_rootx()
        y2 = event.y_root - canvas.winfo_rooty()
        state["rect"] = canvas.create_rectangle(x1, y1, x2, y2, outline="#ff3b30", width=3)

    def finish_selection(event):
        if state["start_x"] is None:
            return
        x1, y1 = state["start_x"], state["start_y"]
        x2, y2 = event.x_root, event.y_root
        # min/abs permettent de dessiner dans n'importe quelle direction.
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width < 2 or height < 2:
            return
        # La nouvelle zone est disponible immediatement pour la prochaine capture.
        with CONFIG_LOCK:
            ROI.update(left=left, top=top, width=width, height=height)
        ui.update_roi_label()
        close_selector()

    for monitor in monitors:
        overlay = tk.Toplevel(ui.root)
        overlays.append(overlay)
        # Fenetre sans bordure, semi-transparente, toujours au-dessus des applications.
        overlay.overrideredirect(True)
        overlay.attributes("-alpha", 0.25)
        overlay.attributes("-topmost", True)
        overlay.geometry(
            f"{monitor['width']}x{monitor['height']}"
            f"{monitor['left']:+d}{monitor['top']:+d}"
        )
        canvas = tk.Canvas(overlay, bg="black", cursor="cross", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<ButtonPress-1>", lambda e, m=monitor, c=canvas: start_selection(e, m, c))
        canvas.bind("<B1-Motion>", draw_selection)
        canvas.bind("<ButtonRelease-1>", finish_selection)
        canvas.bind("<ButtonPress-3>", close_selector)
        overlay.bind("<Escape>", close_selector)
        overlay.focus_force()


class OCRWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CoD Warzone Lobby code Reader")
        self.root.geometry("430x560")
        if ICON_PATH.exists():
            self.root.iconbitmap(default=str(ICON_PATH))

        # Bouton qui ouvre les overlays de selection sur tous les moniteurs.
        tk.Button(self.root, text="Selectionner la zone OCR", command=lambda: select_roi(self)).pack(pady=(12, 5))
        tk.Label(self.root, text="La selection peut etre faite sur n'importe quel ecran.").pack()
        self.roi_var = tk.StringVar()
        tk.Label(self.root, textvariable=self.roi_var).pack(pady=(2, 12))
        self.update_roi_label()

        tk.Label(self.root, text="Apercu de la zone OCR").pack()
        self.image_label = tk.Label(self.root, bg="#202020")
        self.image_label.pack(pady=(4, 14))

        tk.Label(self.root, text="Nombre lu").pack()
        self.number_var = tk.StringVar(value="-")
        # "readonly" conserve la selection/copier de texte sans autoriser la saisie.
        tk.Entry(
            self.root,
            textvariable=self.number_var,
            font=("Arial", 24),
            justify="center",
            state="readonly",
        ).pack(pady=8)
        tk.Button(self.root, text="Copier", command=self.copy_number).pack()

        tk.Label(self.root, text="Historique").pack(pady=(14, 4))
        self.history = tk.Listbox(self.root, height=10)
        self.history.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.history.bind("<<ListboxSelect>>", self.copy_history_item)

    def update_roi_label(self):
        """Affiche la position et la taille actuelles de la zone capturee."""
        with CONFIG_LOCK:
            roi = ROI.copy()
        self.roi_var.set(f"Zone : {roi['width']} x {roi['height']}  |  position : {roi['left']}, {roi['top']}")

    def update_image(self, image):
        """Transforme l'image OpenCV en image Tkinter pour l'apercu."""
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        source = Image.fromarray(rgb)

        # Contrairement a thumbnail(), ce calcul agrandit aussi les petites ROI.
        # L'image est centree sur un fond sombre sans la deformer.
        scale = min(PREVIEW_SIZE[0] / source.width, PREVIEW_SIZE[1] / source.height)
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        preview = source.resize(size, Image.Resampling.LANCZOS)
        frame = Image.new("RGB", PREVIEW_SIZE, "#202020")
        frame.paste(preview, ((PREVIEW_SIZE[0] - size[0]) // 2, (PREVIEW_SIZE[1] - size[1]) // 2))

        photo = ImageTk.PhotoImage(frame)
        self.image_label.configure(image=photo)
        self.image_label.image = photo

    def update_number(self, number):
        """Met a jour le resultat et ajoute uniquement les nouveaux nombres a l'historique."""
        self.number_var.set(number)
        self.history.insert(0, number)

    def copy_number(self):
        pyperclip.copy(self.number_var.get())

    def copy_history_item(self, _event):
        selected = self.history.curselection()
        if selected:
            pyperclip.copy(self.history.get(selected[0]))

    def run(self):
        self.root.mainloop()


def ocr_loop(ui):
    """Capture la ROI dans un thread separe afin de ne pas bloquer Tkinter."""
    last_number = None
    last_preview_at = 0.0
    with mss.MSS() as sct:
        while True:
            # Chaque iteration utilise une copie coherente de la zone actuelle.
            with CONFIG_LOCK:
                roi = ROI.copy()
            try:
                # mss capture l'ecran et OpenCV convertit BGRA (4 canaux) en BGR.
                raw = np.array(sct.grab(roi))
                image = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
            except mss.exception.ScreenShotError:
                time.sleep(DELAY)
                continue

            # L'OCR reste frequent, mais l'affichage de l'aperçu est limite pour
            # ne pas surcharger la fenetre Tkinter.
            now = time.monotonic()
            if now - last_preview_at >= PREVIEW_DELAY:
                ui.root.after(0, ui.update_image, image.copy())
                last_preview_at = now
            number = read_number(image)
            if number and number != last_number:
                # root.after execute la mise a jour dans le thread de Tkinter.
                ui.root.after(0, ui.update_number, number)
                last_number = number
            time.sleep(DELAY)


if __name__ == "__main__":
    # Tkinter tourne dans le thread principal ; la capture/OCR tourne en arriere-plan.
    ui = OCRWindow()
    threading.Thread(target=ocr_loop, args=(ui,), daemon=True).start()
    ui.run()
