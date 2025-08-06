import sys
import os
import json
import platform
import subprocess
import signal
import time
import re

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QRadioButton,
    QButtonGroup, QLineEdit, QFileDialog, QMessageBox, QFrame, QSizePolicy
    # QProgressBar artık kullanılmıyor
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QPixmap

# Ayar dosyası yolu (kullanıcının ana dizininde gizli dosya)
def get_original_user_home():
    if platform.system() in ("Linux", "Darwin"):
        sudo_user = os.getenv('SUDO_USER')
        if sudo_user:
            import pwd
            try:
                user_info = pwd.getpwnam(sudo_user)
                return user_info.pw_dir
            except KeyError:
                pass
    return os.path.expanduser("~")

ORIGINAL_USER_HOME = get_original_user_home()
SETTINGS_FILE = os.path.join(ORIGINAL_USER_HOME, ".zeus_raw_copy_settings.json")

# Yetki yükseltme komutu denemesi
def run_privileged_command(command_parts, error_message):
    commands_to_try = []

    commands_to_try.append(['pkexec'] + command_parts)
    if platform.system() == "Linux":
        if os.path.exists('/usr/bin/gksudo'):
            commands_to_try.append(['gksudo', '--preserve-env', '--message', 'Yönetici yetkileri gerekli:'] + command_parts)
        if os.path.exists('/usr/bin/kdesudo'):
            commands_to_try.append(['kdesudo', '--preserve-env', '--comment', 'Yönetici yetkileri gerekli:'] + command_parts)
    commands_to_try.append(['sudo'] + command_parts)

    last_error = ""
    for cmd in commands_to_try:
        try:
            process = subprocess.Popen(cmd,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       bufsize=1, # Line-buffered output
                                       universal_newlines=True) # Text mode for line reading
            return process
        except FileNotFoundError as e:
            last_error = f"Komut bulunamadı: {e.filename}. Deneniyor: {' '.join(cmd)}"
            continue
        except Exception as e:
            last_error = f"Yetkili komut çalıştırılırken hata: {e}. Deneniyor: {' '.join(cmd)}"
            continue

    raise Exception(f"{error_message}\nHiçbir yetki yükseltme yöntemi çalışmadı: {last_error}")

# İşlem yapan iş parçacığı
class CloningWorker(QThread):
    finished = pyqtSignal(int)
    error = pyqtSignal(str)
    stopped = pyqtSignal()
    progress_update = pyqtSignal(str) # dd'nin ham çıktısını gönderecek

    def __init__(self, source, target):
        super().__init__()
        self.source = source
        self.target = target
        self.process = None
        self._stop_requested = False
        self.stderr_buffer = "" # stderr çıktısını biriktirmek için

    def run(self):
        # bs=4M, daha hızlı kopyalama için blok boyutunu artırır.
        # conv=sync,noerror, okuma hatalarını görmezden gelip senkronize yazar.
        # status=progress, dd'nin ilerleme çıktısı vermesini sağlar.
        command_parts = ['/usr/bin/dd', f'if={self.source}', f'of={self.target}', 'bs=4M', 'conv=sync,noerror', 'status=progress']

        try:
            # Yetkili komutu çalıştır
            self.process = run_privileged_command(command_parts, "Disk işlemi için yetki yükseltilemedi.")

            while True:
                if self._stop_requested:
                    if self.process:
                        if platform.system() in ("Linux", "Darwin"):
                            self.process.send_signal(signal.SIGINT) # Ctrl+C gibi sinyal gönder
                        else:
                            self.process.terminate() # Windows için işlemi sonlandır
                        self.process.wait(timeout=5) # İşlemin bitmesini bekle
                    self.stopped.emit() # Durduruldu sinyalini gönder
                    return

                # stderr'den mevcut tüm veriyi oku
                # Küçük bir okuma boyutu kullanmak, daha sık güncelleme yapmamıza yardımcı olabilir
                output = self.process.stderr.read(1) 
                if output:
                    self.stderr_buffer += output
                    # Eğer '\r' veya '\n' karakteri gördüysek, satırı işle
                    if output == '\r' or output == '\n':
                        if self.stderr_buffer.strip(): # Boş satırları yoksay
                            self.progress_update.emit(self.stderr_buffer.strip())
                        self.stderr_buffer = "" # Tamponu sıfırla
                else:
                    # Karakter gelmediyse ve süreç bittiyse döngüden çık
                    if self.process.poll() is not None:
                        # Kalan çıktıyı da işleyelim (genellikle son satırda \n olmayabilir)
                        if self.stderr_buffer.strip():
                            self.progress_update.emit(self.stderr_buffer.strip())
                        break
                
                time.sleep(0.01) # GUI'nin yanıt verebilir kalması için kısa bir bekleme

            return_code = self.process.returncode
            if return_code == 0:
                self.finished.emit(return_code) # Başarılı dönüş kodu
            elif return_code < 0 and abs(return_code) == signal.SIGINT:
                self.stopped.emit() # Kullanıcı durdurdu
            else:
                # dd'nin son hata mesajını al
                stderr_final_output = self.process.stderr.read()
                full_error_message = f"İşlem hata ile sona erdi (Kod: {return_code}).\nHata Mesajı: {stderr_final_output.strip() or self.stderr_buffer.strip()}"
                self.error.emit(full_error_message) # Hata sinyalini gönder
        except Exception as e:
            self.error.emit(f"Disk işlemi başlatılırken beklenmedik bir hata oluştu: {e}")
        finally:
            self.process = None # Süreci temizle

    def stop(self):
        self._stop_requested = True

class ZeusRawCopyApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("zeus raw copy")
        # self.setFixedSize(720, 560) # Bu satırı yorum satırı yaptık veya sildik
        
        # Pencerenin yeniden boyutlandırılabilir olmasını sağlıyoruz
        # Ve amblemin görünürlüğünü sağlamak için minimum yüksekliği arttırıyoruz.
        self.setMinimumSize(720, 600) # Genişlik 720, minimum yükseklik 600

        self.cloning_worker = None
        self.last_save_directory = ORIGINAL_USER_HOME
        self.last_img_open_directory = ORIGINAL_USER_HOME
        self.total_bytes_to_copy = 0

        self.setup_ui()
        self.load_settings()

        self.all_disks_info = self.get_disk_list()
        self.populate_disk_comboboxes()
        self.toggle_input_fields()

        QApplication.instance().aboutToQuit.connect(self.on_exit)


    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(10)

        info_text = "Bu program, disklerinizin yedeğini ham olarak seçtiğiniz yere kaydeder veya bir imaj dosyasını diske yazar. Seçimlerinizi dikkatlice yapın."
        self.info_label = QLabel(info_text)
        self.info_label.setWordWrap(True)
        main_layout.addWidget(self.info_label)
        main_layout.addSpacing(10)

        # Radio butonlar
        action_and_logo_layout = QHBoxLayout()
        action_and_logo_layout.setSpacing(15)

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
        radio_button_v_layout.addStretch()

        action_and_logo_layout.addLayout(radio_button_v_layout)

        self.logo_label = QLabel(self)
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zeus.png')
        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            if not pixmap.isNull():
                max_logo_width = 150
                max_logo_height = 150
                pixmap = pixmap.scaled(max_logo_width, max_logo_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.logo_label.setPixmap(pixmap)
                self.logo_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                action_and_logo_layout.addWidget(self.logo_label)
        main_layout.addLayout(action_and_logo_layout)
        main_layout.addSpacing(15)

        # Kaynak seçimleri
        source_frame = QFrame()
        source_frame.setFrameShape(QFrame.StyledPanel)
        source_frame.setContentsMargins(10, 10, 10, 10)
        source_layout = QVBoxLayout(source_frame)
        source_layout.setSpacing(5)

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

        # Hedef seçimleri
        target_frame = QFrame()
        target_frame.setFrameShape(QFrame.StyledPanel)
        target_frame.setContentsMargins(10, 10, 10, 10)
        target_layout = QVBoxLayout(target_frame)
        target_layout.setSpacing(5)

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

        # QProgressBar kaldırıldı, sadece status_label kaldı
        self.status_label = QLabel("Hazır.")
        self.status_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.status_label)
        main_layout.addSpacing(15)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        button_width = 140
        button_height = 40

        self.start_button = QPushButton("İşlemi Başlat")
        self.start_button.setFixedSize(button_width, button_height)
        self.start_button.clicked.connect(self.start_operation)
        button_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("İşlemi Durdur")
        self.stop_button.setFixedSize(button_width, button_height)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_cloning)
        button_layout.addWidget(self.stop_button)

        self.exit_button = QPushButton("Çıkış")
        self.exit_button.setFixedSize(button_width, button_height)
        self.exit_button.clicked.connect(self.close)
        button_layout.addWidget(self.exit_button)
        
        self.about_button = QPushButton("Hakkında")
        self.about_button.setFixedSize(button_width, button_height)
        self.about_button.clicked.connect(self.show_about_dialog)
        button_layout.addWidget(self.about_button)

        main_layout.addLayout(button_layout)

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    settings = json.load(f)
                    if 'last_save_directory' in settings and os.path.isdir(settings['last_save_directory']):
                        self.last_save_directory = settings['last_save_directory']
                    if 'last_img_open_directory' in settings and os.path.isdir(settings['last_img_open_directory']):
                        self.last_img_open_directory = settings['last_img_open_directory']
            except Exception:
                pass

    def save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, 'w') as f:
                json.dump({
                    'last_save_directory': self.last_save_directory,
                    'last_img_open_directory': self.last_img_open_directory
                }, f)
        except Exception as e:
            print(f"Ayarlar kaydedilirken hata oluştu: {e}")

    def get_disk_list(self):
        if platform.system() != "Linux":
            QMessageBox.warning(self, "Uyarı", "Disk listesi sadece Linux sistemlerinde desteklenmektedir.")
            return []

        command_parts = ['/usr/bin/lsblk', '-dpn', '-o', 'NAME,SIZE,MODEL']
        try:
            # Popen ile yetkili komutu çalıştırın
            process = run_privileged_command(command_parts, "Disk listesini almak için yetki yükseltilemedi.")
            stdout, stderr = process.communicate(timeout=10) # timeout ekleyelim

            if process.returncode != 0:
                QMessageBox.warning(self, "Hata", f"Disk listesi alınırken hata oluştu:\n{stderr.strip()}")
                return []

            lines = stdout.strip().split('\n')
            disks = []
            for line in lines:
                parts = line.split(None, 2)
                if len(parts) == 3:
                    name, size, model = parts
                elif len(parts) == 2:
                    name, size = parts
                    model = "" # Modeli boş bırak
                else:
                    continue # Geçersiz satırları atla
                if not name.startswith('/dev/'):
                    name = '/dev/' + name
                disks.append({'name': name, 'size': size, 'model': model})
            return disks
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Disk listesi alınırken beklenmedik bir hata oluştu: {e}")
            return []

    def populate_disk_comboboxes(self):
        self.source_disk_combobox.clear()
        self.target_disk_combobox.clear()

        for disk in self.all_disks_info:
            display_text = f"{disk['name']} ({disk['size']}) {disk['model']}"
            self.source_disk_combobox.addItem(display_text, disk['name'])
            self.target_disk_combobox.addItem(display_text, disk['name'])

    def toggle_input_fields(self):
        action = self.action_type_group.checkedId()
        # 1: disk->img, 2: disk->disk, 3: img->disk

        self.source_disk_combobox.setEnabled(action in (1, 2))
        self.source_disk_label.setEnabled(action in (1, 2))

        self.source_img_lineedit.setEnabled(action == 3)
        self.source_img_label.setEnabled(action == 3)
        self.source_img_browse_button.setEnabled(action == 3)

        self.target_img_lineedit.setEnabled(action == 1)
        self.target_img_label.setEnabled(action == 1)
        self.target_img_browse_button.setEnabled(action == 1)

        self.target_disk_combobox.setEnabled(action in (2, 3))
        self.target_disk_label.setEnabled(action in (2, 3))

    def select_source_image_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "İmaj Dosyası Seç", self.last_img_open_directory, "İmaj Dosyaları (*.img);;Tüm Dosyalar (*)")
        if path:
            self.source_img_lineedit.setText(path)
            self.last_img_open_directory = os.path.dirname(path)

    def select_target_image_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Kaydedilecek İmaj Dosyasını Seç", self.last_save_directory, "İmaj Dosyaları (*.img);;Tüm Dosyalar (*)")
        if path:
            if not path.lower().endswith('.img'):
                path += '.img'
            self.target_img_lineedit.setText(path)
            self.last_save_directory = os.path.dirname(path)

    def on_source_disk_selected(self, index):
        pass

    def on_target_disk_selected(self, index):
        pass

    def start_operation(self):
        action = self.action_type_group.checkedId()
        if self.cloning_worker is not None and self.cloning_worker.isRunning():
            QMessageBox.warning(self, "Uyarı", "Başka bir işlem zaten devam ediyor.")
            return

        source = None
        target = None
        self.total_bytes_to_copy = 0 # Her yeni işlemde sıfırla (artık kullanılmasa da durabilir)

        if action == 1: # Disk -> img
            source = self.source_disk_combobox.currentData()
            target = self.target_img_lineedit.text().strip()
            if not source:
                QMessageBox.warning(self, "Hata", "Kaynak disk seçin.")
                return
            if not target:
                QMessageBox.warning(self, "Hata", "Hedef imaj dosyası seçin.")
                return
            # Disk boyutunu almak için: (artık çubuk yok ama bilgi için tutabiliriz)
            for disk in self.all_disks_info:
                if disk['name'] == source:
                    self.total_bytes_to_copy = self.parse_size_to_bytes(disk['size'])
                    break
            if self.total_bytes_to_copy == 0:
                QMessageBox.warning(self, "Hata", "Kaynak disk boyutu belirlenemedi veya geçersiz.")
                return
        elif action == 2: # Disk -> disk
            source = self.source_disk_combobox.currentData()
            target = self.target_disk_combobox.currentData()
            if not source:
                QMessageBox.warning(self, "Hata", "Kaynak disk seçin.")
                return
            if not target:
                QMessageBox.warning(self, "Hata", "Hedef disk seçin.")
                return
            if source == target:
                QMessageBox.warning(self, "Hata", "Kaynak ve hedef disk aynı olamaz.")
                return
            # Disk boyutunu almak için:
            for disk in self.all_disks_info:
                if disk['name'] == source:
                    self.total_bytes_to_copy = self.parse_size_to_bytes(disk['size'])
                    break
            if self.total_bytes_to_copy == 0:
                QMessageBox.warning(self, "Hata", "Kaynak disk boyutu belirlenemedi veya geçersiz.")
                return
        else: # Img -> disk
            source = self.source_img_lineedit.text().strip()
            target = self.target_disk_combobox.currentData()
            if not source or not os.path.isfile(source):
                QMessageBox.warning(self, "Hata", "Geçerli bir kaynak imaj dosyası seçin.")
                return
            if not target:
                QMessageBox.warning(self, "Hata", "Hedef disk seçin.")
                return
            try:
                self.total_bytes_to_copy = os.path.getsize(source)
            except OSError as e:
                QMessageBox.warning(self, "Hata", f"Kaynak imaj dosyasının boyutu alınamadı: {e}")
                return

        confirm_text = f"Seçiminiz:\nKaynak: {source}\nHedef: {target}\n\n<b>!!! DİKKAT: Hedef diskin/dosyanın tüm içeriği silinecek ve üzerine yazılacaktır. !!!</b>\n\nDevam etmek istiyor musunuz?"
        reply = QMessageBox.question(self, "Onayla", confirm_text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("İşlem başlatılıyor...") # Sadece durum etiketini güncelle

        self.cloning_worker = CloningWorker(source, target)
        self.cloning_worker.finished.connect(self.on_clone_finished)
        self.cloning_worker.error.connect(self.on_clone_error)
        self.cloning_worker.stopped.connect(self.on_clone_stopped)
        self.cloning_worker.progress_update.connect(self.update_progress) # Sadece metin güncellenecek
        self.cloning_worker.start()

    def parse_size_to_bytes(self, size_str):
        size_str = size_str.strip().upper()
        if not size_str:
            return 0

        # Birimden önce sayısal kısmı bul
        match = re.match(r'(\d+\.?\d*)\s*([KMGTPE]?B)?', size_str)
        if not match:
            return 0

        num = float(match.group(1))
        unit = match.group(2) if match.group(2) else ''

        # Byte, Kilobyte, Megabyte, Gigabyte, Terabyte, Petabyte, Exabyte
        # lsblk genellikle K, M, G, T, P, E kullanır.
        if 'E' in unit:
            return int(num * (1024**6))
        elif 'P' in unit:
            return int(num * (1024**5))
        elif 'T' in unit:
            return int(num * (1024**4))
        elif 'G' in unit:
            return int(num * (1024**3))
        elif 'M' in unit:
            return int(num * (1024**2))
        elif 'K' in unit:
            return int(num * 1024)
        elif 'B' in unit or not unit: # 'B' varsa veya birim yoksa (sadece sayı)
            return int(num)
        return 0

    def update_progress(self, raw_output):
        # Sadece ham çıktıyı durum etiketinde göster
        self.status_label.setText(f"İşlem sürüyor: {raw_output}")

    def stop_cloning(self):
        if self.cloning_worker and self.cloning_worker.isRunning():
            reply = QMessageBox.question(self, "İşlemi Durdur", "İşlemi durdurmak istediğinize emin misiniz? Bu veri kaybına neden olabilir ve hedef disk kararsız durumda kalabilir.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.cloning_worker.stop()
                self.stop_button.setEnabled(False)
                self.status_label.setText("İşlem durduruluyor...") # Sadece durum etiketini güncelle

    def on_clone_finished(self, code):
        self.status_label.setText("İşlem başarıyla tamamlandı.") # Sadece durum etiketini güncelle
        QMessageBox.information(self, "Başarılı", "İşlem başarıyla tamamlandı.")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def on_clone_error(self, message):
        self.status_label.setText(f"Hata: {message}") # Sadece durum etiketini güncelle
        QMessageBox.critical(self, "Hata", f"İşlem sırasında hata oluştu:\n{message}")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def on_clone_stopped(self):
        self.status_label.setText("İşlem kullanıcı tarafından durduruldu.") # Sadece durum etiketini güncelle
        QMessageBox.information(self, "Durduruldu", "İşlem kullanıcı tarafından durduruldu.")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def show_about_dialog(self):
        about_text = (
            "<h3>Zeus Raw Copy v1.0</h3>"
        "<p>Bu program, disklerinizin ham (raw) kopyalarını oluşturmak ve imaj dosyalarını disklere yazmak için tasarlanmıştır.</p>"
        "<p><b>Author:</b> @zeus</p>"
        "<p><b>Github:</b> <a href='https://github.com/shampuan/'>https://github.com/shampuan/</a></p>"
        "<p>Lütfen disk işlemleri yaparken dikkatli olun, yanlış seçimler veri kaybına neden olabilir.</p>"
        )
        QMessageBox.information(self, "Hakkında", about_text)

    def on_exit(self):
        self.save_settings()
        if self.cloning_worker and self.cloning_worker.isRunning():
            self.cloning_worker.stop()
            self.cloning_worker.wait(2000) # İş parçacığının bitmesini bekle

def main():
    app = QApplication(sys.argv)
    window = ZeusRawCopyApp()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
