import signal
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QMainWindow,
    QApplication,
    QLabel,
    QPushButton,
    QTextEdit,
)

from ProxyZ import (
    MainWindow,
    ManualInterfacesList,
    ensure_local_build_id_file,
)


class ProxyZMiniWindow(MainWindow):
    """Version GUI Proxy-only: même panneau interfaces que ProxyZ, sans ZRotate."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProxyZmini - 0 Co / 0 Prox")
        self.resize(420, 900)
        self.setMinimumWidth(420)
        self.setMaximumWidth(420)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("mainWidget")
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        interfaces_panel = QWidget(central)
        interfaces_panel.setObjectName("interfacesPanel")
        interfaces_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        left = QVBoxLayout(interfaces_panel)
        left.setContentsMargins(12, 12, 12, 12)
        left.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)

        self.global_settings_button = QPushButton()
        self.global_settings_button.setObjectName("globalSettingsButton")
        self.global_settings_button.setText("⚙ Paramètres réseau")
        self.global_settings_button.setToolTip("Ouvrir les connexions réseau Windows")
        self.global_settings_button.setFixedHeight(34)
        self.global_settings_button.clicked.connect(
            lambda: self.on_interface_settings_requested("")
        )
        self.add_remote_iface_button = QPushButton("+")
        self.add_remote_iface_button.setObjectName("addRemoteIfaceButton")
        self.add_remote_iface_button.setToolTip("Ajouter une interface réseau distante")
        self.add_remote_iface_button.setFixedHeight(34)
        self.add_remote_iface_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_remote_iface_button.clicked.connect(self._on_add_remote_interface)
        title_row.addWidget(self.global_settings_button)
        title_row.addWidget(self.add_remote_iface_button)
        title_row.addStretch(1)
        left.addLayout(title_row)

        self.auto_container = QWidget()
        self.auto_layout = QVBoxLayout(self.auto_container)
        self.auto_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_layout.setSpacing(6)
        self.auto_container.setMaximumWidth(620)
        left.addWidget(self.auto_container)

        self.manual_list = ManualInterfacesList()
        self.manual_list.order_changed.connect(self.on_manual_order_changed)
        self.manual_list.user_interaction.connect(self._mark_user_interaction)

        manual_scroll = QScrollArea()
        manual_scroll.setWidgetResizable(True)
        manual_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        manual_scroll.setFrameShape(QFrame.NoFrame)

        manual_container = QWidget()
        manual_container_layout = QVBoxLayout(manual_container)
        manual_container_layout.setContentsMargins(0, 0, 0, 0)
        manual_container_layout.addWidget(self.manual_list)
        manual_scroll.setWidget(manual_container)
        left.addWidget(manual_scroll, 1)

        main_layout.addWidget(interfaces_panel)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setObjectName("logBox")
        self.log_box.hide()

        # Même style de base que ProxyZ pour garder le rendu identique côté proxy.
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #011324;
            }
            QWidget#mainWidget {
                background-color: #011324;
            }
            QWidget#interfacesPanel {
                background-color: #011324;
                border: none;
            }
            QLabel#titleLabel {
                color: #ecf0f1;
                font-size: 17px;
                font-weight: 600;
            }
            QLabel#globalStatus {
                color: #ecf0f1;
                font-size: 13px;
                font-weight: 500;
            }
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QListWidget {
                background-color: #02172e;
                border-radius: 12px;
                border: 1px solid rgba(31, 41, 55, 0.9);
            }
            QPushButton#globalSettingsButton, QToolButton#globalSettingsButton {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #3b82f6,
                    stop:0.6 #2563eb,
                    stop:1 #1d4ed8
                );
                color: #f9fafb;
                border-radius: 17px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 600;
                border: 1px solid rgba(15, 23, 42, 0.9);
            }
            QPushButton#globalSettingsButton:hover {
                background-color: #2563eb;
            }
            QPushButton#addRemoteIfaceButton {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #34d399,
                    stop:0.55 #22c55e,
                    stop:1 #16a34a
                );
                color: #f9fafb;
                border-radius: 17px;
                border: 1px solid rgba(15, 23, 42, 0.9);
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.02em;
                min-height: 34px;
                max-height: 34px;
                padding: 0 18px;
            }
            QPushButton#addRemoteIfaceButton:hover {
                background-color: #22c55e;
                color: #ffffff;
            }
            """
        )

    def _update_window_title(self):
        try:
            online_count = sum(
                1
                for info in self.interface_manager.interfaces.values()
                if info.is_up and info.local_ip and info.public_ip
            )
        except Exception:
            online_count = 0

        self.setWindowTitle(
            f"ProxyZmini - {online_count} Co / {self.active_proxies} Prox"
        )

    # Neutralisation des hooks ZRotate appelés par la classe parente.
    # MainWindow évolue souvent; on no-op explicitement tout ce qui touche
    # l'UI/runtime ZRotate pour garder ProxyZmini totalement indépendant.
    def _update_zrotate_interfaces_list(self):
        return

    def _set_quarantine_ui_stopped(self):
        return

    def _on_quarantine_updated(self, names_obj: object):
        return

    def _on_pool_state_updated(self, state_obj: object):
        return

    def _on_quota_stats_updated(self, stats: dict):
        return

    def _sync_zrotate_row_pool_styles(self):
        return

    def _auto_start_zrotate(self):
        return

    def closeEvent(self, event):
        """Fermeture dédiée ProxyZmini: arrêt propre, sans persistance config."""
        print("[SHUTDOWN][MINI] closeEvent reçu, arrêt de l'application...")
        self._refresh_after_reset_timer.stop()

        # Arrêter tous les proxies proprement
        for name, thread in list(self.proxy_threads.items()):
            try:
                print(f"[SHUTDOWN][MINI] Arrêt proxy pour interface '{name}'...")
                thread.stop()
                if not thread.wait(2000):
                    thread.wait(1000)
            except Exception:
                traceback.print_exc()
        self.proxy_threads.clear()

        # Arrêter InterfaceManager (timers + workers)
        try:
            self.interface_manager.shutdown()
        except Exception:
            traceback.print_exc()

        print("[SHUTDOWN][MINI] Fermeture de la fenêtre principale.")
        QMainWindow.closeEvent(self, event)


def main():
    ensure_local_build_id_file()
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon("mini.ico"))
    app.setStyle("Fusion")
    signal.signal(signal.SIGINT, lambda *args: app.quit())
    window = ProxyZMiniWindow()
    window.show()
    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
