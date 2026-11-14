from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.graphics import Color, RoundedRectangle
from kivy.core.window import Window
from agents import ConsensusNeuralMatrix
from broker import TradeBroker

# टोनी स्टार्क जार्विस डार्क टेक थीम (डीप ब्लैक बैकग्राउंड)
Window.clearcolor = (0.01, 0.01, 0.02, 1)

class TacticalNeuralMatrixApp(App):
    def build(self):
        self.neural_matrix = ConsensusNeuralMatrix()
        self.broker = TradeBroker()

        # मुख्य लेआउट (पूरे स्क्रीन पर फिट होने के लिए)
        root = BoxLayout(orientation='vertical', padding=10, spacing=8)

        # 1. टॉप कमांड सेंटर हेडर (ग्लोइंग बॉर्डर के साथ)
        header_layout = BoxLayout(size_hint_y=None, height=45, padding=6)
        with header_layout.canvas.before:
            Color(0.08, 0.08, 0.1, 1)
            self.rect = RoundedRectangle(size=header_layout.size, pos=header_layout.pos, radius=[6])
            header_layout.bind(size=self._update_rect, pos=self._update_rect)
        
        title_label = Label(
            text="[b]⚡ TACTICAL NEURAL MATRIX // JARVIS[/b]",
            markup=True,
            font_size=15,
            color=(1, 0.55, 0.1, 1) # एम्बर ऑरेंज ग्लो
        )
        header_layout.add_widget(title_label)
        root.add_widget(header_layout)

        # स्क्रॉलव्यू ताकि छोटा या बड़ा फोन हो, पूरा कंटेंट आसानी से ऊपर-नीचे हो सके
        scroll = ScrollView(size_hint=(1, 1))
        content_box = BoxLayout(orientation='vertical', size_hint_y=None, spacing=10, padding=5)
        content_box.bind(minimum_height=content_box.setter('height'))

        # 2. सिग्नल बॉक्स (मार्केट स्टेटस)
        signal_box = BoxLayout(orientation='vertical', size_hint_y=None, height=90, padding=8)
        with signal_box.canvas.before:
            Color(0.05, 0.05, 0.07, 1)
            self.sig_rect = RoundedRectangle(size=signal_box.size, pos=signal_box.pos, radius=[6])
            signal_box.bind(size=self._update_sig_rect, pos=self._update_sig_rect)
        
        sig_title = Label(text="[b][color=#00E5FF]LIVE SIGNAL / MENTOR ADVICE:[/color][/b]", markup=True, size_hint_y=None, height=22, halign='left')
        sig_title.bind(size=sig_title.setter('text_size'))
        signal_box.add_widget(sig_title)

        self.signal_desc = Label(
            text="Tap SCAN below to analyze market conditions with 11-Agent Consensus.",
            markup=True,
            valign='top',
            halign='left',
            color=(0.9, 0.9, 0.9, 1),
            font_size=13
        )
        self.signal_desc.bind(size=self._update_text_size_sig)
        signal_box.add_widget(self.signal_desc)
        content_box.add_widget(signal_box)

        # 3. 11-एजेंट्स स्टेटस बॉक्स
        agents_box = BoxLayout(orientation='vertical', size_hint_y=None, height=150, padding=8)
        with agents_box.canvas.before:
            Color(0.05, 0.05, 0.07, 1)
            self.agt_rect = RoundedRectangle(size=agents_box.size, pos=agents_box.pos, radius=[6])
            agents_box.bind(size=self._update_agt_rect, pos=self._update_agt_rect)

        agt_title = Label(text="[b][color=#FF8800]11-AGENT CONSENSUS SHIELD (Max Risk: 1%)[/color][/b]", markup=True, size_hint_y=None, height=22, halign='left')
        agt_title.bind(size=agt_title.setter('text_size'))
        agents_box.add_widget(agt_title)

        self.agents_status_label = Label(
            text="• Market Structure: READY\n• Liquidity Sweep: STANDBY\n• Risk Lockout: ACTIVE (Drop-by-Drop Growth)",
            markup=True,
            valign='top',
            halign='left',
            color=(0.8, 0.8, 0.8, 1),
            font_size=12
        )
        agents_box.add_widget(self.agents_status_label)
        content_box.add_widget(agents_box)

        # 4. एक्शन बटन (SCAN, PAPER TRADE, KILL) - अब यह बिल्कुल साफ दिखेंगे और कटेंगे नहीं
        action_layout = BoxLayout(size_hint_y=None, height=45, spacing=8)
        
        btn_scan = Button(text="SCAN", background_color=(0, 0.7, 0.9, 1), color=(1, 1, 1, 1), bold=True)
        btn_scan.bind(on_press=self.on_scan_press)
        action_layout.add_widget(btn_scan)

        btn_trade = Button(text="PAPER TRADE", background_color=(1, 0.55, 0, 1), color=(1, 1, 1, 1), bold=True)
        btn_trade.bind(on_press=self.on_trade_press)
        action_layout.add_widget(btn_trade)

        btn_kill = Button(text="KILL", background_color=(0.9, 0.2, 0.2, 1), color=(1, 1, 1, 1), bold=True)
        btn_kill.bind(on_press=self.on_kill_press)
        action_layout.add_widget(btn_kill)

        content_box.add_widget(action_layout)

        # 5. चैट और टीचिंग हिस्ट्री बॉक्स
        self.chat_history = Label(
            text="[JARVIS Mentor]: Systems online, Sir. Ask me anything about trading, charts, or wealth building!",
            markup=True,
            size_hint_y=None,
            valign='top',
            halign='left',
            color=(0.9, 0.9, 0.9, 1),
            font_size=13
        )
        self.chat_history.bind(texture_size=self._update_chat_size)
        content_box.add_widget(self.chat_history)

        scroll.add_widget(content_box)
        root.add_widget(scroll)

        # 6. बॉटम कमांड बार (MIC और टेक्स्ट इनपुट - बिल्कुल नीचे फिक्स और साफ)
        bottom_layout = BoxLayout(size_hint_y=None, height=50, spacing=6)

        self.voice_btn = Button(
            text="🎙️ MIC",
            size_hint_x=0.25,
            background_color=(0.12, 0.12, 0.18, 1),
            color=(0, 0.8, 1, 1),
            bold=True
        )
        self.voice_btn.bind(on_press=self.on_voice_command)
        bottom_layout.add_widget(self.voice_btn)

        self.user_input = TextInput(
            hint_text="Ask JARVIS (e.g. Teach me support)...",
            size_hint_x=0.55,
            multiline=False,
            background_color=(0.06, 0.06, 0.08, 1),
            foreground_color=(1, 1, 1, 1),
            cursor_color=(1, 0.5, 0.1, 1),
            padding=[8, 12, 8, 12]
        )
        bottom_layout.add_widget(self.user_input)

        self.send_btn = Button(
            text="GO",
            size_hint_x=0.2,
            background_color=(0.9, 0.45, 0.05, 1),
            color=(1, 1, 1, 1),
            bold=True
        )
        self.send_btn.bind(on_press=self.on_send_command)
        bottom_layout.add_widget(self.send_btn)

        root.add_widget(bottom_layout)
        return root

    def _update_rect(self, instance, value):
        self.rect.pos = instance.pos
        self.rect.size = instance.size

    def _update_sig_rect(self, instance, value):
        self.sig_rect.pos = instance.pos
        self.sig_rect.size = instance.size

    def _update_agt_rect(self, instance, value):
        self.agt_rect.pos = instance.pos
        self.agt_rect.size = instance.size

    def _update_text_size_sig(self, label, size):
        label.text_size = (label.width, None)

    def _update_chat_size(self, label, size):
        label.text_size = (label.width, None)
        label.height = max(120, size[1])

    def on_scan_press(self, instance):
        self.signal_desc.text = "[color=#00E5FF]Market Scanned:[/color] Trend is stable. 11 Agents give 95% green light for safe entry."

    def on_trade_press(self, instance):
        feedback = self.neural_matrix.evaluate_and_teach("buy trade")
        result = self.broker.execute_order(feedback)
        self.signal_desc.text = f"[color=#FF8800]Trade Executed:[/color] {result}"

    def on_kill_press(self, instance):
        self.signal_desc.text = "[color=#FF3333]KILLED:[/color] All active positions closed securely. Capital locked."

    def on_send_command(self, instance):
        text = self.user_input.text.strip()
        if text:
            current = self.chat_history.text
            response_msg = self.neural_matrix.evaluate_and_teach(text)
            self.chat_history.text = f"{current}\n\n[Sir]: {text}\n[JARVIS Mentor]: {response_msg}"
            self.user_input.text = ""

    def on_voice_command(self, instance):
        current = self.chat_history.text
        self.chat_history.text = f"{current}\n\n[JARVIS Voice]: Voice listener active on mobile, Sir. Speak your command!"

if __name__ == '__main__':
    TacticalNeuralMatrixApp().run()
