[app]

title = Tactical Neural Matrix
package.name = tacticalneuralmatrix
package.domain = org.tdn
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = bin, tests, venv, .git, .github, .buildozer
version = 3.0.0

# agents.py and broker.py use only the Python standard library.
# plyer needs pyjnius and android on the phone (text-to-speech and speech recognition).
requirements = python3,kivy==2.3.0,plyer,pyjnius,android

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
android.minapi = 21
android.ndk = 25b
android.ndk_api = 21
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.wakelock = False
android.presplash_color = #010103
android.logcat_filters = *:S python:D
android.sdk_path =
android.ndk_path =

p4a.branch = v2023.09.16
p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 0
bin_dir = ./bin
build_dir = ./.buildozer
