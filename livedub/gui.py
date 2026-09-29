"""PySide6 desktop UI."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .audio.devices import IS_MAC, IS_WINDOWS, Device, default_output, list_outputs, list_sources
from .config import CONFIG_FILE, ENGINE_CASCADE, ENGINE_GEMINI, ENGINE_TRANSLATE, Settings
from .engines import ENGINE_LABELS
from .languages import AUTO, LANGUAGES, TRANSLATE_MODEL_OUTPUTS, display_name
from .session import SWITCH_NOTE, DubSession, plan_input


class Bridge(QObject):
    """Carries session events (emitted on worker threads) to the Qt main thread."""

    event = Signal(str, object)


class LiveText(QPlainTextEdit):
    """Append-only transcript view that understands streaming deltas and interim text."""

    GAP_S = 1.5

    def __init__(self, placeholder: str):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(400)
        self.setPlaceholderText(placeholder)
        font = self.font()
        font.setPointSize(font.pointSize() + 2)
        self.setFont(font)
        self._open = False
        self._last = 0.0
        self._partial = 0

    def _insert(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _drop_partial(self) -> None:
        if self._partial:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, self._partial)
            cursor.removeSelectedText()
            self._partial = 0

    def _new_paragraph(self) -> None:
        if not self.document().isEmpty():
            self._insert("\n")

    def delta(self, text: str) -> None:
        self._drop_partial()
        now = time.monotonic()
        if self._open and now - self._last > self.GAP_S:
            self._open = False
        if not self._open:
            text = text.lstrip()
            if not text:
                return
            self._new_paragraph()
            self._open = True
        self._insert(text)
        self._last = now

    def end(self) -> None:
        self._open = False

    def partial(self, text: str) -> None:
        self._drop_partial()
        self._open = False
        chunk = ("\n" if not self.document().isEmpty() else "") + text
        self._insert(chunk)
        self._partial = len(chunk.encode("utf-16-le")) // 2  # Qt cursor positions count UTF-16 units

    def line(self, text: str) -> None:
        self._drop_partial()
        self._open = False
        self._new_paragraph()
        self._insert(text)


# -- settings dialog ----------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ayarlar")
        self.setMinimumWidth(560)
        self._settings = settings
        self._getters: dict[str, callable] = {}

        tabs = QTabWidget()
        tabs.addTab(self._keys_tab(), "API anahtarları")
        tabs.addTab(self._gemini_tab(), "Gemini")
        tabs.addTab(self._openai_tab(), "OpenAI motorları")
        tabs.addTab(self._cascade_tab(), "Kaskad motoru")
        tabs.addTab(self._advanced_tab(), "Gelişmiş")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # widget factories bound to a Settings attribute
    def _text(self, attr: str, secret: bool = False) -> QLineEdit:
        w = QLineEdit(str(getattr(self._settings, attr)))
        if secret:
            w.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._getters[attr] = lambda: w.text().strip()
        return w

    def _editable(self, attr: str, options: list[str]) -> QComboBox:
        w = QComboBox()
        w.setEditable(True)
        w.addItems(options)
        w.setCurrentText(str(getattr(self._settings, attr)))
        self._getters[attr] = lambda: w.currentText().strip()
        return w

    def _choice(self, attr: str, options: list[tuple[str, str]]) -> QComboBox:
        w = QComboBox()
        for value, label in options:
            w.addItem(label, value)
        index = w.findData(getattr(self._settings, attr))
        w.setCurrentIndex(max(index, 0))
        self._getters[attr] = lambda: w.currentData()
        return w

    def _check(self, attr: str, label: str) -> QCheckBox:
        w = QCheckBox(label)
        w.setChecked(bool(getattr(self._settings, attr)))
        self._getters[attr] = w.isChecked
        return w

    def _int(self, attr: str, lo: int, hi: int, step: int, suffix: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setSuffix(suffix)
        w.setValue(int(getattr(self._settings, attr)))
        self._getters[attr] = w.value
        return w

    def _float(self, attr: str, lo: float, hi: float, step: float, suffix: str = "", scale: float = 1.0) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(2)
        w.setSuffix(suffix)
        w.setValue(float(getattr(self._settings, attr)) * scale)
        self._getters[attr] = lambda: w.value() / scale
        return w

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: gray;")
        return label

    def _keys_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow("Gemini", self._text("gemini_api_key", secret=True))
        link = QLabel('<a href="https://aistudio.google.com/apikey">Ücretsiz Gemini anahtarı al (Google AI Studio)</a>')
        link.setOpenExternalLinks(True)
        form.addRow("", link)
        form.addRow("OpenAI", self._text("openai_api_key", secret=True))
        form.addRow("Deepgram", self._text("deepgram_api_key", secret=True))
        form.addRow("ElevenLabs", self._text("elevenlabs_api_key", secret=True))
        form.addRow("DeepL", self._text("deepl_api_key", secret=True))
        form.addRow(self._note(
            f"Anahtarlar düz metin olarak {CONFIG_FILE} dosyasında saklanır. Boş bırakılırsa ortam değişkenleri "
            "(GEMINI_API_KEY, OPENAI_API_KEY, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, DEEPL_API_KEY) kullanılır.\n"
            "Gemini motoru için yalnızca Gemini anahtarı, OpenAI motorları için yalnızca OpenAI anahtarı yeterlidir. "
            "Kaskad motoru Deepgram + (OpenAI veya DeepL) "
            "+ (OpenAI veya ElevenLabs) ister."
        ))
        return page

    def _gemini_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow("Live Translate modeli", self._editable("gemini_translate_model", ["gemini-3.5-live-translate-preview"]))
        form.addRow(self._note(
            "Konuşmacı konuşurken çevirir; kaynak dili kendisi algılar, ses tonu konuşmacıya uyarlanır. "
            "Dublaj sesi yakalanan kaynağa geri karışırsa model kendi sesini tekrar çevirip döngüye girer; bu yüzden "
            "sistem sesi için 'Tüm sistem sesi (dublaj hariç)' kaynağını kullanın.\n"
            "Ücret: ücretsiz katmanda ses girişi bedava, çeviri sesi dakikada yaklaşık 0,018 $. Ücretsiz katmanda "
            "gönderilen içerik Google tarafından ürün geliştirmede kullanılabilir."
        ))
        return page

    def _openai_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        common = QGroupBox("Ortak")
        form = QFormLayout(common)
        form.addRow("Gürültü azaltma", self._choice("noise_reduction", [
            ("", "Kapalı (sistem sesi için önerilir)"),
            ("near_field", "Yakın mikrofon / kulaklık mikrofonu"),
            ("far_field", "Uzak mikrofon / oda"),
        ]))
        layout.addWidget(common)

        translate = QGroupBox("Realtime Translate (eşzamanlı çeviri)")
        form = QFormLayout(translate)
        form.addRow("Model", self._editable("translate_model", ["gpt-realtime-translate"]))
        form.addRow("Kaynak transkript modeli", self._editable("translate_transcribe_model", ["gpt-realtime-whisper"]))
        form.addRow(self._note("Ses tonu konuşmacıya göre otomatik uyarlanır; ses seçimi yoktur. Çıkış dilleri: "
                               + ", ".join(sorted(TRANSLATE_MODEL_OUTPUTS))))
        layout.addWidget(translate)

        realtime = QGroupBox("Realtime GPT (tercüman modu)")
        form = QFormLayout(realtime)
        form.addRow("Model", self._editable("realtime_model", ["gpt-realtime-1.5", "gpt-realtime-2", "gpt-realtime-2.1", "gpt-realtime", "gpt-realtime-mini"]))
        form.addRow("Ses", self._editable("realtime_voice", ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]))
        form.addRow("Konuşma hızı", self._float("realtime_speed", 0.25, 1.5, 0.05, "x"))
        form.addRow("Akıl yürütme", self._choice("realtime_reasoning", [
            ("", "Model varsayılanı"), ("minimal", "minimal (en hızlı)"), ("low", "low"),
        ]))
        form.addRow("Kaynak transkript modeli", self._editable("realtime_transcribe_model", ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"]))
        form.addRow("Cümle sonu algılama", self._choice("vad_type", [
            ("server_vad", "Sessizliğe göre (server_vad) — en hızlı"),
            ("semantic_vad", "Anlama göre (semantic_vad) — daha doğal ama yavaş"),
        ]))
        form.addRow("Sessizlik eşiği", self._int("vad_silence_ms", 150, 1500, 50, " ms"))
        form.addRow("VAD hassasiyeti", self._float("vad_threshold", 0.1, 0.95, 0.05))
        form.addRow("En uzun parça", self._float("max_turn_s", 3.0, 20.0, 0.5, " sn"))
        form.addRow(self._note("Akıl yürütme ayarı yalnızca gpt-realtime-2 ailesinde geçerlidir. 'En uzun parça': "
                               "konuşmacı hiç durmasa bile bu sürede bir çeviri tetiklenir."))
        layout.addWidget(realtime)
        layout.addStretch()
        return page

    def _cascade_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow("Deepgram modeli", self._editable("deepgram_model", ["nova-3", "nova-2"]))
        form.addRow("Cümle sonu (endpointing)", self._int("deepgram_endpointing_ms", 100, 2000, 50, " ms"))
        form.addRow("Çevirmen", self._choice("translator", [("openai", "OpenAI (bağlama duyarlı LLM)"), ("deepl", "DeepL (çok hızlı)")]))
        form.addRow("Çeviri modeli (OpenAI)", self._editable("translate_text_model", ["gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini"]))
        form.addRow("Seslendirme (TTS)", self._choice("tts_provider", [("openai", "OpenAI TTS"), ("elevenlabs", "ElevenLabs")]))
        form.addRow("OpenAI TTS modeli", self._editable("openai_tts_model", ["gpt-4o-mini-tts", "tts-1"]))
        form.addRow("OpenAI TTS sesi", self._editable("openai_tts_voice", ["coral", "marin", "cedar", "alloy", "ash", "ballad", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]))
        form.addRow("ElevenLabs modeli", self._editable("elevenlabs_model", ["eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2"]))
        form.addRow("ElevenLabs ses ID", self._text("elevenlabs_voice_id"))
        form.addRow(self._check("adaptive_speed", "Gecikme birikirse seslendirmeyi hızlandır"))
        form.addRow(self._note("Otomatik kaynak dil için nova-3 gerekir (language=multi). Belirli bir dil seçerseniz "
                               "o dili destekleyen modeli kullanın; Türkçe kaynak için nova-2 en garantili seçenektir."))
        return page

    def _advanced_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(self._check("show_source_text", "Duyulan konuşmanın metnini göster (OpenAI'da ek transkripsiyon ücreti)"))
        form.addRow("Geri besleme koruması", self._choice("feedback_guard", [
            ("auto", "Otomatik (önerilir)"), ("on", "Her zaman açık"), ("off", "Kapalı"),
        ]))
        form.addRow("Dublajda orijinali kıs", self._float("duck_level", 0.0, 100.0, 5.0, " %", scale=100.0))
        form.addRow(self._note(
            "Geri besleme koruması: dublaj sesi yakalanan kaynağa geri sızabiliyorsa (aynı hoparlörün sistem sesi ya da "
            "hoparlörü duyan bir mikrofon), dublaj çalarken giriş susturulur. Bu sırada konuşulanlar kaçabilir; en iyi "
            "sonuç için kulaklık veya sanal kablo (VB-CABLE / BlackHole) kullanın. Otomatik modda, kaynak çıkışla aynı "
            "cihazın sistem sesiyse bunun yerine 'Tüm sistem sesi (dublaj hariç)' yakalanır ve giriş susturulmaz.\n"
            "'Dublajda orijinali kıs': orijinal sesi de çalıyorsanız, dublaj konuşurken orijinalin bu seviyeye inmesi."
        ))
        return page

    def result_settings(self) -> Settings:
        return replace(self._settings, **{attr: get() for attr, get in self._getters.items()})


# -- main window --------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"LiveDub {__version__} — Canlı Çeviri ve Dublaj")
        self.resize(980, 680)
        self.settings = Settings.load()
        self.session: DubSession | None = None
        self._sources: list[Device] = []
        self._outputs: list[Device] = []

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)

        if IS_WINDOWS:
            try:
                from .audio.sessions import recover

                recover()  # apps left turned down by a run that did not shut down cleanly
            except Exception:
                pass
        self._build()
        self._refresh_devices()
        self._load_widgets()
        self._update_hints()

    # -- layout ---------------------------------------------------------------------------------
    def _build(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        audio = QGroupBox("Ses")
        grid = QGridLayout(audio)
        self.source_combo = QComboBox()
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setToolTip("Cihaz listesini yenile")
        self.refresh_btn.clicked.connect(self._refresh_devices)
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(8)
        self.output_combo = QComboBox()
        self.dub_slider = QSlider(Qt.Orientation.Horizontal)
        self.dub_slider.setRange(0, 150)
        self.orig_slider = QSlider(Qt.Orientation.Horizontal)
        self.orig_slider.setRange(0, 100)
        self.dub_value = QLabel()
        self.orig_value = QLabel()
        for w in (self.dub_value, self.orig_value):
            w.setMinimumWidth(40)

        grid.addWidget(QLabel("Kaynak"), 0, 0)
        grid.addWidget(self.source_combo, 0, 1, 1, 4)
        grid.addWidget(self.refresh_btn, 0, 5)
        grid.addWidget(self.level_bar, 1, 1, 1, 4)
        grid.addWidget(QLabel("Çıkış"), 2, 0)
        grid.addWidget(self.output_combo, 2, 1, 1, 4)
        grid.addWidget(QLabel("Dublaj sesi"), 3, 0)
        grid.addWidget(self.dub_slider, 3, 1)
        grid.addWidget(self.dub_value, 3, 2)
        grid.addWidget(QLabel("Orijinal ses"), 3, 3)
        grid.addWidget(self.orig_slider, 3, 4)
        grid.addWidget(self.orig_value, 3, 5)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        layout.addWidget(audio)

        translate = QGroupBox("Çeviri")
        row = QHBoxLayout(translate)
        self.src_lang = QComboBox()
        self.src_lang.addItem(display_name(AUTO), AUTO)
        self.tgt_lang = QComboBox()
        for code, name, _ in LANGUAGES:
            self.src_lang.addItem(name, code)
            self.tgt_lang.addItem(name, code)
        self.engine_combo = QComboBox()
        for key, label in ENGINE_LABELS.items():
            self.engine_combo.addItem(label, key)
        self.settings_btn = QPushButton("Ayarlar…")
        self.settings_btn.clicked.connect(self._open_settings)
        row.addWidget(self.src_lang)
        row.addWidget(QLabel("→"))
        row.addWidget(self.tgt_lang)
        row.addSpacing(16)
        row.addWidget(QLabel("Motor"))
        row.addWidget(self.engine_combo, 1)
        row.addWidget(self.settings_btn)
        layout.addWidget(translate)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #b36b00;")
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Ses testi")
        self.test_btn.setToolTip("API kullanmadan çıkışta bip çalar ve giriş seviyesini gösterir")
        self.test_btn.clicked.connect(self._audio_test)
        self.start_btn = QPushButton()
        self.start_btn.setMinimumHeight(44)
        bold = QFont(self.start_btn.font())
        bold.setPointSize(bold.pointSize() + 3)
        bold.setBold(True)
        self.start_btn.setFont(bold)
        self.start_btn.clicked.connect(self._toggle)
        buttons.addWidget(self.test_btn)
        buttons.addWidget(self.start_btn, 1)
        layout.addLayout(buttons)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        for title, attr, placeholder in (
            ("Duyulan", "source_view", "Kaynak konuşmanın metni burada görünecek"),
            ("Çeviri (dublaj)", "target_view", "Seslendirilen çeviri burada görünecek"),
        ):
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            header = QLabel(title)
            header.setStyleSheet("font-weight: bold;")
            view = LiveText(placeholder)
            setattr(self, attr, view)
            box_layout.addWidget(header)
            box_layout.addWidget(view)
            splitter.addWidget(box)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.state_label = QLabel("Hazır")
        self.latency_label = QLabel("Gecikme: –")
        self.backlog_label = QLabel("Kuyruk: –")
        status = self.statusBar()
        status.addPermanentWidget(self.latency_label)
        status.addPermanentWidget(self.backlog_label)
        status.addWidget(self.state_label, 1)

        self.dub_slider.valueChanged.connect(self._volumes_changed)
        self.orig_slider.valueChanged.connect(self._volumes_changed)
        for combo in (self.source_combo, self.output_combo, self.tgt_lang, self.engine_combo):
            combo.currentIndexChanged.connect(self._update_hints)
        self._set_running(False)

    # -- device & settings plumbing -------------------------------------------------------------
    def _refresh_devices(self) -> None:
        current_src = self.source_combo.currentData() if self.source_combo.count() else self.settings.source_device
        current_out = self.output_combo.currentData() if self.output_combo.count() else self.settings.output_device
        self._sources = list_sources()
        self._outputs = list_outputs()
        default = default_output()

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("Varsayılan mikrofon", "")
        for dev in self._sources:
            self.source_combo.addItem(dev.label, dev.id)
        self._select(self.source_combo, current_src)
        self.source_combo.blockSignals(False)

        self.output_combo.blockSignals(True)
        self.output_combo.clear()
        self.output_combo.addItem(f"Varsayılan çıkış ({default.name})" if default else "Varsayılan çıkış", "")
        for dev in self._outputs:
            self.output_combo.addItem(dev.name, dev.id)
        self._select(self.output_combo, current_out)
        self.output_combo.blockSignals(False)
        self._update_hints()

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _load_widgets(self) -> None:
        s = self.settings
        self._select(self.source_combo, s.source_device)
        self._select(self.output_combo, s.output_device)
        self._select(self.src_lang, s.source_lang)
        self._select(self.tgt_lang, s.target_lang)
        self._select(self.engine_combo, s.engine)
        self.dub_slider.setValue(round(s.dub_volume * 100))
        self.orig_slider.setValue(round(s.original_volume * 100))
        self._volumes_changed()

    def _read_widgets(self) -> None:
        self.settings = replace(
            self.settings,
            source_device=self.source_combo.currentData(),
            output_device=self.output_combo.currentData(),
            source_lang=self.src_lang.currentData(),
            target_lang=self.tgt_lang.currentData(),
            engine=self.engine_combo.currentData(),
            dub_volume=self.dub_slider.value() / 100,
            original_volume=self.orig_slider.value() / 100,
        )

    def _save(self) -> None:
        self._read_widgets()
        try:
            self.settings.save()
        except OSError as exc:
            self.statusBar().showMessage(f"Ayarlar kaydedilemedi: {exc}", 8000)

    def _volumes_changed(self) -> None:
        self.dub_value.setText(f"{self.dub_slider.value()}%")
        self.orig_value.setText(f"{self.orig_slider.value()}%")
        if self.session:
            self.session.set_volumes(self.dub_slider.value() / 100, self.orig_slider.value() / 100)

    def _current_source(self) -> Device:
        dev_id = self.source_combo.currentData()
        for dev in self._sources:
            if dev.id == dev_id:
                return dev
        return Device("", "default microphone", "input")

    def _current_output(self) -> Device | None:
        dev_id = self.output_combo.currentData()
        for dev in self._outputs:
            if dev.id == dev_id:
                return dev
        return default_output()

    def _update_hints(self) -> None:
        hints = []
        engine = self.engine_combo.currentData()
        target = self.tgt_lang.currentData()
        source = self._current_source()
        if engine == ENGINE_TRANSLATE and target not in TRANSLATE_MODEL_OUTPUTS:
            hints.append(f"⚠ Realtime Translate modeli {display_name(target)} konuşamıyor. "
                         f"{display_name(target)} dublaj için 'OpenAI Realtime GPT' veya 'Kaskad' motorunu seçin.")
        elif engine in (ENGINE_TRANSLATE, ENGINE_GEMINI):
            hints.append("ℹ Bu motor kaynak dili kendisi algılar ve konuşmacı konuşurken çevirir (en düşük gecikme).")
        if engine == ENGINE_CASCADE and self.src_lang.currentData() == AUTO:
            hints.append("ℹ Kaskad motorunda kaynak dili seçmek tanıma doğruluğunu artırır.")
        _, guard, note = plan_input(self.settings.feedback_guard, source if source.id else None, self._current_output())
        if note:
            hints.append(f"ℹ {SWITCH_NOTE}")
        elif guard:
            hints.append("ℹ Dublaj sesi kaynağa geri sızabilir: dublaj çalarken giriş susturulacak. Kesintisiz çeviri için "
                         "kulaklık ya da sanal kablo kullanın (README).")
        if IS_WINDOWS and source.kind == "loopback":
            hints.append("ℹ Orijinal ses: çalışırken diğer uygulamalar bu seviyeye kısılır (%0 = yalnızca dublaj duyulur), "
                         "çeviri etkilenmez; durdurunca sesler eski hâline döner.")
        if IS_MAC and source.virtual:
            hints.append(f"ℹ Mac'in ses çıkışı {source.name} olmalı (Sistem Ayarları › Ses › Çıkış). LiveDub'ın Çıkış'ı "
                         "hoparlör ya da kulaklık olmalı. Orijinali de duymak için 'Orijinal ses' kaydırıcısını açın.")
        if IS_MAC and not any(d.virtual for d in self._sources):
            hints.append("ℹ macOS'ta sistem sesini (YouTube, Zoom…) çevirmek için ücretsiz BlackHole sürücüsünü kurun (README).")
        self.hint.setText("\n".join(hints))
        self.hint.setVisible(bool(hints))

    def _open_settings(self) -> None:
        self._read_widgets()
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings = dialog.result_settings()
            self._save()
            self._update_hints()

    # -- run control ----------------------------------------------------------------------------
    def _set_running(self, running: bool, test: bool = False) -> None:
        for w in (self.source_combo, self.output_combo, self.refresh_btn, self.src_lang, self.tgt_lang,
                  self.engine_combo, self.settings_btn, self.test_btn):
            w.setEnabled(not running)
        self.start_btn.setEnabled(not test)
        self.start_btn.setText("■  Durdur" if running and not test else "▶  Başlat")
        color = "#c0392b" if running and not test else "#1e8449"
        self.start_btn.setStyleSheet(f"QPushButton {{ background: {color}; color: white; border-radius: 6px; }}"
                                     "QPushButton:disabled { background: #888; }")
        if not running:
            self.level_bar.setValue(0)

    def _start_session(self, test: bool) -> None:
        self._save()
        self.session = DubSession(replace(self.settings), lambda kind, data: self.bridge.event.emit(kind, data), test_mode=test)
        self.session.start()
        self._set_running(True, test=test)
        self.state_label.setText("Başlatılıyor…")

    def _toggle(self) -> None:
        if self.session:
            self.session.stop()
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Durduruluyor…")
            return
        self.latency_label.setText("Gecikme: –")
        self._start_session(test=False)

    def _audio_test(self) -> None:
        if not self.session:
            self._start_session(test=True)

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "level":
            self.level_bar.setValue(int(max(0.0, min(100.0, (data["db"] + 60.0) / 60.0 * 100.0))))
        elif kind == "source_delta":
            self.source_view.delta(data["text"])
        elif kind == "source_partial":
            self.source_view.partial(data["text"])
        elif kind == "source_line":
            self.source_view.line(data["text"])
        elif kind == "source_end":
            self.source_view.end()
        elif kind == "target_delta":
            self.target_view.delta(data["text"])
        elif kind == "target_end":
            self.target_view.end()
        elif kind == "latency":
            self.latency_label.setText(f"Gecikme: {data['ms']} ms")
        elif kind == "backlog":
            self.backlog_label.setText(f"Kuyruk: {data['seconds']:.1f} sn")
        elif kind == "status":
            self.state_label.setText(data["text"])
        elif kind == "running":
            self.state_label.setText("Çalışıyor")
        elif kind == "error":
            self.statusBar().showMessage(f"⚠ {data['text']}", 10000)
        elif kind == "notice":
            self.statusBar().showMessage(f"ℹ {data['text']}", 15000)
        elif kind == "input_silent":
            self.hint.setText(f"⚠ {data['text']}")
            self.hint.setVisible(True)
        elif kind == "input_ok":
            self._update_hints()
        elif kind == "fatal":
            QMessageBox.warning(self, "LiveDub", data["text"])
        elif kind == "stopped":
            self.session = None
            self._set_running(False)
            self.state_label.setText("Durduruldu")
            self.backlog_label.setText("Kuyruk: –")
            self._update_hints()  # drop a silent-input warning from the finished run

    def closeEvent(self, event) -> None:
        self._save()
        if self.session:
            self.session.stop()
            self.session.join(3)
        super().closeEvent(event)


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LiveDub")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "icon.png")))
    window = MainWindow()
    window.show()
    return app.exec()
