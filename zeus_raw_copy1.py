import sys
import subprocess
import os
import json
import platform
import time
import signal

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QMessageBox, QButtonGroup, QRadioButton, QFrame, QLineEdit,
    QSizePolicy, QFileDialog, QSpacerItem, QSizePolicy, QMainWindow # QMainWindow'u import ettik
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QMargins
from PyQt5.QtGui import QPixmap, QImage

# --- Orijinal kullanıcının ev dizinini bulma fonksiyonu ---
def get_original_user_home():
    """
    Program normal kullanıcı olarak başladığında kendi ev dizinini döndürür.
    SUDO_USER kontrolü, programın sudo ile başlatıldığı eski durumlar için bir fallback'tir.
    """
    if platform.system() == "Linux" or platform.system() == "Darwin":
        sudo_user = os.getenv('SUDO_USER')
        if sudo_user:
            try:
                import pwd
                user_info = pwd.getpwnam(sudo_user)
                return user_info.pw_dir
            except KeyError:
                pass
    return os.path.expanduser("~")

# Dosya geçmişi ve ayarlar için yollar
ORIGINAL_USER_HOME = get_original_user_home()
SETTINGS_FILE = os.path.join(ORIGINAL_USER_HOME, ".zeus_raw_copy_settings.json")


# --- Yardımcı Fonksiyon: Komutu yönetici yetkileriyle çalıştırma ---
def run_privileged_command(command_parts, error_message):
    """
    Belirtilen komutu pkexec, gksudo veya kdesudo kullanarak yönetici yetkileriyle çalıştırır.
    """
    admin_commands = []

    # Deneme 1: pkexec (Tercih edilen, PolicyKit kuralını bulmaya çalışır)
    admin_commands.append(['pkexec'] + command_parts)

    # Deneme 2: gksudo (GNOME/GTK tabanlı sistemler için fallback)
    if platform.system() == "Linux" and os.path.exists('/usr/bin/gksudo'):
        admin_commands.append(['gksudo', '--preserve-env', '--message', 'Yönetici yetkileri gerekli:'] + command_parts)

    # Deneme 3: kdesudo (KDE tabanlı sistemler için fallback)
    if platform.system() == "Linux" and os.path.exists('/usr/bin/kdesudo'):
        admin_commands.append(['kdesudo', '--preserve-env', '--comment', 'Yönetici yetkileri gerekli:'] + command_parts)

    # Deneme 4: sudo (Terminalde parola ister, son çare, GUI için ideal değil)
    admin_commands.append(['sudo'] + command_parts)

    last_error = ""
    for cmd in admin_commands:
        try:
            # print(f"Denenen yetki yükseltme komutu: {' '.join(cmd)}")
            process = subprocess.Popen(cmd,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        bufsize=1,
                                        universal_newlines=True)

            return process
        except FileNotFoundError as e:
            last_error = f"Komut bulunamadı: {e.filename}. Deneniyor: {' '.join(cmd)}"
            # print(f"Hata (FileNotFoundError): {last_error}")
            continue
        except Exception as e:
            last_error = f"Yetkili komut çalıştırılırken genel hata: {e}. Deneniyor: {' '.join(cmd)}"
            # print(f"Hata (Genel): {last_error}")
            continue

    raise Exception(f"{error_message}\nHiçbir yetki yükseltme yöntemi çalışmadı: {last_error}")


class CloningWorker(QThread):
    finished = pyqtSignal(int)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, source, target):
        super().__init__()
        self.source = source
        self.target = target
        self.process = None
        self._stop_requested = False

    def run(self):
        command_parts = ['/usr/bin/dd', f'if={self.source}', f'of={self.target}', 'bs=4M', 'conv=sync,noerror']

        try:
            self.process = run_privileged_command(command_parts, "Disk işlemi için yetki yükseltilemedi.")

            while self.process.poll() is None:
                if self._stop_requested:
                    if platform.system() == "Linux" or platform.system() == "Darwin":
                        self.process.send_signal(signal.SIGINT)
                    else:
                        self.process.terminate()

                    self.process.wait()
                    self.stopped.emit()
                    return

                time.sleep(0.5)

            return_code = self.process.returncode
            if return_code == 0:
                self.finished.emit(return_code)
            elif return_code < 0 and abs(return_code) == signal.SIGINT:
                self.stopped.emit()
            else:
                stderr_output = self.process.stderr.read()
                self.error.emit(f"İşlem hata ile sona erdi (Kod: {return_code}).\nHata Mesajı: {stderr_output.strip()}")
        except Exception as e:
            self.error.emit(f"Disk işlemi başlatılırken beklenmedik bir hata oluştu: {e}")
        finally:
            self.process = None

    def stop(self):
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            pass

# QWidget yerine QMainWindow'dan miras alıyoruz
class ZeusRawCopyApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("zeus raw copy")
        self.setFixedSize(720, 560) # QMainWindow için setFixedSize

        self.cloning_worker = None
        self.last_save_directory = ORIGINAL_USER_HOME
        self.last_img_open_directory = ORIGINAL_USER_HOME

        self.setup_ui()
        self.load_settings()

        self.all_disks_info = self.get_disk_list()
        self.populate_disk_comboboxes()
        self.toggle_input_fields()

        QApplication.instance().aboutToQuit.connect(self.on_exit)

    def setup_ui(self):
        # Tüm ana içeriği barındıracak merkezi bir widget oluşturuyoruz
        central_widget = QWidget()
        self.setCentralWidget(central_widget) # QMainWindow'ın merkezi widget'ını ayarlıyoruz

        main_layout = QVBoxLayout(central_widget) # Layout'ı merkezi widget'a bağlıyoruz
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(10)

        # Menü çubuğunu kaldırdık ve yerine Hakkında butonu ekleyeceğiz.
        # Eğer yine de menü çubuğu isteseydik, self.menuBar() ile oluşturup
        # menüleri ona eklememiz yeterliydi, layout'a eklemezdik.
        # Bu kez menü çubuğu tamamen kaldırıldı.


        # --- Bilgi Metni ---
        info_text = "Bu program, disklerinizin yedeğini ham olarak seçtiğiniz yere kaydeder veya bir imaj dosyasını diske yazar. Seçimlerinizi dikkatlice yapın."
        self.info_label = QLabel(info_text)
        self.info_label.setWordWrap(True)
        main_layout.addWidget(self.info_label)
        main_layout.addSpacing(10)

        # --- İşlem Tipi Seçimi ve Amblem için Yatay Layout ---
        action_and_logo_layout = QHBoxLayout()
        action_and_logo_layout.setSpacing(15) # Radyo butonları ile amblem arasındaki boşluk

        # Radyo Butonları için Dikey Layout (solda)
        radio_button_v_layout = QVBoxLayout()
        self.action_type_group = QButtonGroup(self)
        self.disk_to_img_radio = QRadioButton("Diski .img uzantılı dosyaya klonla")
        self.disk_to_img_radio.setChecked(True)
        self.disk_to_disk_radio = QRadioButton("Diski doğrudan diske klonla")
        self.img_to_disk_radio = QRadioButton("İmaj dosyasını diske yaz")

        self.action_type_group.addButton(self.disk_to_img_radio, 1)
        self.action_type_group.addButton(self.disk_to_disk_radio, 2)
        self.action_type_group.addButton(self.img_to_disk_radio, 3)
        self.action_type_group.buttonClicked.connect(self.toggle_input_fields)

        radio_button_v_layout.addWidget(self.disk_to_img_radio)
        radio_button_v_layout.addWidget(self.disk_to_disk_radio)
        radio_button_v_layout.addWidget(self.img_to_disk_radio)
        radio_button_v_layout.addStretch() # Radyo butonlarını üste yaslamak için

        action_and_logo_layout.addLayout(radio_button_v_layout)

        # Amblem (sağda)
        self.logo_label = QLabel(self)
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zeus.png')

        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            if not pixmap.isNull():
                max_logo_width = 150
                max_logo_height = 150

                pixmap = pixmap.scaled(max_logo_width, max_logo_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.logo_label.setPixmap(pixmap)
                self.logo_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter) # Sağ ortaya hizala
                action_and_logo_layout.addWidget(self.logo_label)
            else:
                print(f"Hata: Amblem dosyası '{logo_path}' geçerli bir resim değil.")
        else:
            print(f"Hata: Amblem dosyası '{logo_path}' bulunamadı.")

        main_layout.addLayout(action_and_logo_layout)
        main_layout.addSpacing(15)

        # --- Kaynak Seçim Alanları ---
        source_frame = QFrame()
        source_frame.setFrameShape(QFrame.StyledPanel)
        source_frame.setContentsMargins(10, 10, 10, 10)
        source_layout = QVBoxLayout(source_frame)
        source_layout.setSpacing(5)

        # Kaynak Disk Seçimi
        source_disk_layout = QHBoxLayout()
        self.source_disk_label = QLabel("Kaynak Disk:")
        self.source_disk_label.setFixedWidth(120)
        self.source_disk_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        source_disk_layout.addWidget(self.source_disk_label)

        self.source_disk_combobox = QComboBox()
        self.source_disk_combobox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.source_disk_combobox.currentIndexChanged.connect(self.on_source_disk_selected)
        source_disk_layout.addWidget(self.source_disk_combobox)
        source_layout.addLayout(source_disk_layout)

        # Kaynak İmaj Dosyası Seçimi
        source_img_layout = QHBoxLayout()
        self.source_img_label = QLabel("Kaynak İmaj (.img):")
        self.source_img_label.setFixedWidth(120)
        self.source_img_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        source_img_layout.addWidget(self.source_img_label)

        self.source_img_lineedit = QLineEdit()
        self.source_img_lineedit.setPlaceholderText("Yazılacak imaj dosyasının yolunu girin veya göz atın.")
        self.source_img_lineedit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        source_img_layout.addWidget(self.source_img_lineedit)

        self.source_img_browse_button = QPushButton("Gözat")
        self.source_img_browse_button.setFixedWidth(80)
        self.source_img_browse_button.clicked.connect(self.select_source_image_file)
        source_img_layout.addWidget(self.source_img_browse_button)
        source_layout.addLayout(source_img_layout)

        main_layout.addWidget(source_frame)
        main_layout.addSpacing(15)


        # --- Hedef Seçim Alanları ---
        target_frame = QFrame()
        target_frame.setFrameShape(QFrame.StyledPanel)
        target_frame.setContentsMargins(10, 10, 10, 10)
        target_layout = QVBoxLayout(target_frame)
        target_layout.setSpacing(5)

        # Hedef .img Dosyası Seçimi
        target_img_layout = QHBoxLayout()
        self.target_img_label = QLabel("Hedef (.img):")
        self.target_img_label.setFixedWidth(120)
        self.target_img_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        target_img_layout.addWidget(self.target_img_label)

        self.target_img_lineedit = QLineEdit()
        self.target_img_lineedit.setPlaceholderText("Kaydedilecek dosya yolunu girin veya göz atın.")
        self.target_img_lineedit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        target_img_layout.addWidget(self.target_img_lineedit)

        self.target_img_browse_button = QPushButton("Gözat")
        self.target_img_browse_button.setFixedWidth(80)
        self.target_img_browse_button.clicked.connect(self.select_target_image_file)
        target_img_layout.addWidget(self.target_img_browse_button)
        target_layout.addLayout(target_img_layout)

        # Hedef Disk Seçimi
        target_disk_layout = QHBoxLayout()
        self.target_disk_label = QLabel("Hedef Disk:")
        self.target_disk_label.setFixedWidth(120)
        self.target_disk_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        target_disk_layout.addWidget(self.target_disk_label)

        self.target_disk_combobox = QComboBox()
        self.target_disk_combobox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.target_disk_combobox.currentIndexChanged.connect(self.on_target_disk_selected)
        target_disk_layout.addWidget(self.target_disk_combobox)
        target_layout.addLayout(target_disk_layout)

        main_layout.addWidget(target_frame)
        main_layout.addStretch()

        # --- Butonlar ---
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        # Yeni Hakkında Butonu
        self.about_button = QPushButton("Hakkında")
        self.about_button.setMinimumHeight(40)
        self.about_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.about_button.clicked.connect(self.show_about_dialog)
        button_layout.addWidget(self.about_button)

        self.start_button = QPushButton("İşlemi Başlat")
        self.start_button.setMinimumHeight(40)
        self.start_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        button_layout.addWidget(self.start_button)
        self.start_button.clicked.connect(self.start_operation)

        self.stop_button = QPushButton("İşlemi Durdur")
        self.stop_button.setMinimumHeight(40)
        self.stop_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        button_layout.addWidget(self.stop_button)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_cloning)

        self.exit_button = QPushButton("Çıkış")
        self.exit_button.setMinimumHeight(40)
        self.exit_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        button_layout.addWidget(self.exit_button)
        self.exit_button.clicked.connect(self.close)

        main_layout.addLayout(button_layout)

    def load_settings(self):
        """Ayarları (son kaydedilen dizinler) yükler."""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    settings = json.load(f)
                    if 'last_save_directory' in settings and \
                       os.path.isdir(settings['last_save_directory']):
                        self.last_save_directory = settings['last_save_directory']
                    if 'last_img_open_directory' in settings and \
                       os.path.isdir(settings['last_img_open_directory']):
                        self.last_img_open_directory = settings['last_img_open_directory']
            except (json.JSONDecodeError, FileNotFoundError):
                pass

    def save_settings(self):
        """Ayarları (son kaydedilen dizinler) kaydeder."""
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, 'w') as f:
                json.dump({
                    'last_save_directory': self.last_save_directory,
                    'last_img_open_directory': self.last_img_open_directory
                }, f)
        except IOError as e:
            print(f"Ayarlar kaydedilirken hata oluştu: {e}")

    def get_disk_list(self):
        if platform.system() != "Linux":
            QMessageBox.warning(self, "Uyarı", "Bu özellik sadece Linux sistemlerinde desteklenmektedir (lsblk komutu).")
            return []

        command_parts = ['/usr/bin/lsblk', '-dpn', '-o', 'NAME,SIZE,MODEL']
        try:
            process = run_privileged_command(command_parts, "Disk listesini almak için yetki yükseltilemedi.")
            stdout, stderr = process.communicate(timeout=20)

            if process.returncode == 0:
                lines = stdout.strip().split('\n')
                disks_info = []
                for line in lines:
                    if not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) >= 3:
                        name = parts[0]
                        size = parts[1]
                        model = ' '.join(parts[2:])
                        disks_info.append({'name': name, 'size': size, 'model': model, 'display_name': f"{name} ({size} - {model})"})
                return disks_info
            else:
                QMessageBox.critical(self, "Hata", f"Diskler listelenirken bir sorun oluştu:\n{stderr.strip()}")
                return []
        except subprocess.TimeoutExpired:
            process.kill()
            QMessageBox.critical(self, "Hata", "Disk listesi alma işlemi zaman aşımına uğradı. Parolayı zamanında girmemiş olabilirsiniz.")
            return []
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Disk listesi alınırken beklenmedik bir hata oluştu: {e}")
            return []

    def populate_disk_comboboxes(self):
        display_names = [disk['display_name'] for disk in self.all_disks_info]
        self.source_disk_combobox.clear()
        self.source_disk_combobox.addItems(display_names)
        self.target_disk_combobox.clear()
        self.target_disk_combobox.addItems(display_names)

        if display_names:
            self.source_disk_combobox.setCurrentIndex(0)
            self.on_source_disk_selected(0)
            self.target_disk_combobox.setCurrentIndex(0)
            self.on_target_disk_selected(0)

    def on_source_disk_selected(self, index):
        selected_display = self.source_disk_combobox.itemText(index)
        self._selected_source_disk_path = None
        for disk in self.all_disks_info:
            if disk['display_name'] == selected_display:
                self._selected_source_disk_path = disk['name']
                break

    def on_target_disk_selected(self, index):
        selected_display = self.target_disk_combobox.itemText(index)
        self._selected_target_disk_path = None
        for disk in self.all_disks_info:
            if disk['display_name'] == selected_display:
                self._selected_target_disk_path = disk['name']
                break

    def select_source_image_file(self):
        file_dialog = QFileDialog(self)
        start_dir = self.last_img_open_directory if os.path.isdir(self.last_img_open_directory) else ORIGINAL_USER_HOME
        file_dialog.setDirectory(start_dir)

        file_dialog.setFileMode(QFileDialog.ExistingFile)
        file_dialog.setNameFilter("Disk Image Files (*.img);;All Files (*.*)")
        file_dialog.setAcceptMode(QFileDialog.AcceptOpen)

        file_path = None
        if file_dialog.exec_():
            selected_files = file_dialog.selectedFiles()
            if selected_files:
                file_path = selected_files[0]

        if file_path:
            self.source_img_lineedit.setText(file_path)
            new_open_directory = os.path.dirname(file_path)
            self.last_img_open_directory = new_open_directory
            self.save_settings()

    def select_target_image_file(self):
        file_dialog = QFileDialog(self)

        start_dir = self.last_save_directory if os.path.isdir(self.last_save_directory) else ORIGINAL_USER_HOME
        file_dialog.setDirectory(start_dir)

        file_dialog.setFileMode(QFileDialog.AnyFile)
        file_dialog.setNameFilter("Disk Image Files (*.img);;All Files (*.*)")
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)

        file_path = None
        if file_dialog.exec_():
            selected_files = file_dialog.selectedFiles()
            if selected_files:
                file_path = selected_files[0]

        if file_path:
            if not file_path.lower().endswith(".img"):
                file_path += ".img"
            self.target_img_lineedit.setText(file_path)

            new_save_directory = os.path.dirname(file_path)
            self.last_save_directory = new_save_directory
            self.save_settings()

    def toggle_input_fields(self):
        selected_action = self.action_type_group.checkedId()

        # Varsayılan olarak tümünü devre dışı bırak
        self.source_disk_label.setEnabled(False)
        self.source_disk_combobox.setEnabled(False)
        self.source_img_label.setEnabled(False)
        self.source_img_lineedit.setEnabled(False)
        self.source_img_browse_button.setEnabled(False)

        self.target_img_label.setEnabled(False)
        self.target_img_lineedit.setEnabled(False)
        self.target_img_browse_button.setEnabled(False)
        self.target_disk_label.setEnabled(False)
        self.target_disk_combobox.setEnabled(False)

        if selected_action == 1: # Diski .img dosyasına klonla
            self.source_disk_label.setEnabled(True)
            self.source_disk_combobox.setEnabled(True)
            self.target_img_label.setEnabled(True)
            self.target_img_lineedit.setEnabled(True)
            self.target_img_browse_button.setEnabled(True)
            self.target_disk_combobox.setCurrentIndex(-1)
            self.source_img_lineedit.setText('')

        elif selected_action == 2: # Diski doğrudan diske klonla
            self.source_disk_label.setEnabled(True)
            self.source_disk_combobox.setEnabled(True)
            self.target_disk_label.setEnabled(True)
            self.target_disk_combobox.setEnabled(True)
            self.target_img_lineedit.setText('')
            self.source_img_lineedit.setText('')

        elif selected_action == 3: # İmaj dosyasını diske yaz
            self.source_img_label.setEnabled(True)
            self.source_img_lineedit.setEnabled(True)
            self.source_img_browse_button.setEnabled(True)
            self.target_disk_label.setEnabled(True)
            self.target_disk_combobox.setEnabled(True)
            self.source_disk_combobox.setCurrentIndex(-1)
            self.target_img_lineedit.setText('')

    def set_gui_state(self, enabled):
        self.start_button.setEnabled(enabled)
        self.stop_button.setEnabled(not enabled)
        self.about_button.setEnabled(enabled) # Hakkında butonu da aktif/pasif edilsin

        self.disk_to_img_radio.setEnabled(enabled)
        self.disk_to_disk_radio.setEnabled(enabled)
        self.img_to_disk_radio.setEnabled(enabled)
        self.exit_button.setEnabled(enabled)

        if enabled:
            self.toggle_input_fields()
        else:
            self.source_disk_label.setEnabled(False)
            self.source_disk_combobox.setEnabled(False)
            self.source_img_label.setEnabled(False)
            self.source_img_lineedit.setEnabled(False)
            self.source_img_browse_button.setEnabled(False)
            self.target_img_label.setEnabled(False)
            self.target_img_lineedit.setEnabled(False)
            self.target_img_browse_button.setEnabled(False)
            self.target_disk_label.setEnabled(False)
            self.target_disk_combobox.setEnabled(False)

    def start_operation(self):
        source = None
        target = None
        selected_action = self.action_type_group.checkedId()

        if selected_action == 1: # Diski .img dosyasına klonla
            source = getattr(self, '_selected_source_disk_path', None)
            target = self.target_img_lineedit.text().strip()
            if not source:
                QMessageBox.critical(self, "Hata", "Lütfen bir kaynak disk seçin.")
                return
            if not target:
                QMessageBox.critical(self, "Hata", "Lütfen bir hedef .img dosyası yolu belirtin.")
                return
            target_dir = os.path.dirname(target)
            if not target_dir:
                target_dir = os.getcwd()
            if not os.path.isdir(target_dir):
                QMessageBox.critical(self, "Hata", f"Hedef klasör '{target_dir}' mevcut değil veya erişilemiyor.")
                return
            confirmation_message = f"'{source}' diskini '{target}' dosyasına klonlamak üzeresiniz. Devam etmek istiyor musunuz?"

        elif selected_action == 2: # Diski doğrudan diske klonla
            source = getattr(self, '_selected_source_disk_path', None)
            target = getattr(self, '_selected_target_disk_path', None)
            if not source:
                QMessageBox.critical(self, "Hata", "Lütfen bir kaynak disk seçin.")
                return
            if not target:
                QMessageBox.critical(self, "Hata", "Lütfen bir hedef disk seçin.")
                return
            if source == target:
                QMessageBox.critical(self, "Hata", "Kaynak disk ile hedef disk aynı olamaz! Bu, veri kaybına yol açacaktır.")
                return
            confirmation_message = (
                f"'{source}' diskini doğrudan '{target}' diskine klonlamak üzeresiniz. "
                f"Hedef diskteki TÜM VERİLER kalıcı olarak silinecektir! Devam etmek istiyor musunuz?"
            )

        elif selected_action == 3: # İmaj dosyasını diske yaz
            source = self.source_img_lineedit.text().strip()
            target = getattr(self, '_selected_target_disk_path', None)
            if not source:
                QMessageBox.critical(self, "Hata", "Lütfen bir kaynak imaj dosyası seçin.")
                return
            if not target:
                QMessageBox.critical(self, "Hata", "Lütfen bir hedef disk seçin.")
                return
            if not os.path.exists(source) or not os.path.isfile(source):
                QMessageBox.critical(self, "Hata", f"Kaynak imaj dosyası '{source}' bulunamadı veya geçerli bir dosya değil.")
                return

            confirmation_message = (
                f"'{source}' imaj dosyasını doğrudan '{target}' diskine yazmak üzeresiniz. "
                f"Hedef diskteki TÜM VERİLER kalıcı olarak silinecektir! Devam etmek istiyor musunuz?"
            )

        else:
            QMessageBox.critical(self, "Hata", "Geçersiz işlem tipi seçimi.")
            return

        response = QMessageBox.question(
            self, "Onay Gerekiyor",
            confirmation_message,
            QMessageBox.Yes | QMessageBox.No
        )
        if response == QMessageBox.No:
            return

        self.set_gui_state(False)

        self.cloning_worker = CloningWorker(source, target)
        self.cloning_worker.finished.connect(self.on_cloning_finished)
        self.cloning_worker.error.connect(self.on_cloning_error)
        self.cloning_worker.stopped.connect(self.on_cloning_stopped)
        self.cloning_worker.start()

        QMessageBox.information(self, "Bilgi", "İşlem başladı. Bu işlem, diskin boyutuna bağlı olarak zaman alabilir. Lütfen bekleyin...")

    def stop_cloning(self):
        if self.cloning_worker and self.cloning_worker.isRunning():
            confirm = QMessageBox.question(self, "İşlemi Durdur",
                                           "Devam eden işlem durdurulacaktır. Bu, eksik bir disk görüntüsüne veya bozuk bir diske neden olabilir. Emin misiniz?",
                                           QMessageBox.Yes | QMessageBox.No)
            if confirm == QMessageBox.Yes:
                self.cloning_worker.stop()
        else:
            QMessageBox.information(self, "Bilgi", "Herhangi bir işlem zaten çalışmıyor.")
            self.set_gui_state(True)

    def on_cloning_finished(self, return_code):
        QMessageBox.information(self, "Başarılı", "İşlem başarıyla tamamlandı.")
        self.set_gui_state(True)

    def on_cloning_error(self, message):
        QMessageBox.critical(self, "Hata", message)
        self.set_gui_state(True)

    def on_cloning_stopped(self):
        QMessageBox.warning(self, "Durduruldu", "İşlem kullanıcı tarafından durduruldu.")
        self.set_gui_state(True)

    # --- Hakkında Diyalog Fonksiyonu ---
    def show_about_dialog(self):
        about_text = (
            "<h3>Zeus Raw Copy v1.0</h3>"
            "<p>Bu program, disklerinizin ham (raw) kopyalarını oluşturmak ve imaj dosyalarını disklere yazmak için tasarlanmıştır.</p>"
            "<p><b>Geliştirici:</b> @zeus</p>"
            "<p><b>Github:</b> <a href='https://github.com/shampuan/'>https://github.com/shampuan/</a></p>"
            "<p>Lütfen disk işlemleri yaparken dikkatli olun, yanlış seçimler veri kaybına neden olabilir.</p>"
        )
        QMessageBox.about(self, "Hakkında: Zeus Raw Copy", about_text)


    def on_exit(self):
        self.save_settings()

        if self.cloning_worker and self.cloning_worker.isRunning():
            reply = QMessageBox.question(self, 'Çıkışı Onayla',
                                         "Devam eden işlem var. Çıkmak istiyor musunuz? İşlem durdurulacaktır.",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return
            else:
                self.cloning_worker.stop()
                self.cloning_worker.wait(2000)
        sys.exit(0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ZeusRawCopyApp()
    window.show()
    sys.exit(app.exec_())
