#!/usr/bin/env bash
# Set up Flutter and the Android SDK so the app is developed and built on this machine (SET-07):
# a dev PC, or the board itself (ARM64 Linux such as the Orange Pi 5 Plus).
#
#   scripts/setup_android_dev.sh                install what is missing (sudo only for packages)
#   scripts/setup_android_dev.sh --no-packages  skip apt (JDK 17, git, curl, unzip, adb; qemu on ARM64)
#   scripts/setup_android_dev.sh --shims-only   ARM64: only re-create the x86-64 tool wrappers
#                                               (build_apk.sh does it before every build)
#
# For the user who runs it: Flutter $FLUTTER_VERSION in $FLUTTER_DIR (a git tag), the Android SDK in
# $ANDROID_HOME (command-line tools, platform, build-tools; the SDK licences are accepted), flutter and
# dart linked into ~/.local/bin. Defaults: FLUTTER_DIR=~/flutter, ANDROID_HOME=~/Android/Sdk.
#
# ARM64 Linux: three tools of an APK build are published by Google for x86-64 only - gen_snapshot (Dart
# to machine code, release builds), aapt2 (Android resources) and the NDK's llvm-strip (takes the debug
# symbols out of the engine library: a 445 MB APK without it). They run under qemu-user with the x86-64
# C library of libc6-amd64-cross and Debian's x86-64 zlib (no binfmt_misc needed): a wrapper next to
# each gen_snapshot in Flutter's engine cache (linux-arm64/, which Flutter looks for but Google does not
# publish), $ANDROID_HOME/arm64-shims/aapt2 (Gradle: android.aapt2FromMavenOverride), and the NDK's
# llvm-strip and llvm-objcopy replaced by wrappers. The empty CMake project Flutter adds to fetch the NDK is left out
# on ARM64 (app/android/app/build.gradle.kts): CMake and the NDK's compilers do not run here.
# With less than 12 GB of RAM, ~/.gradle/gradle.properties also caps Gradle's memory (3 GB, no Kotlin
# daemon, an idle Gradle daemon exits after 15 min).
# The release key is not set up here: copy app/android/key.properties and the keystore it names from
# the machine that has them (gitignored), or build_apk.sh creates a new one.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLUTTER_VERSION="${FLUTTER_VERSION:-3.38.5}"     # the version the project is built and tested with
FLUTTER_DIR="${FLUTTER_DIR:-$HOME/flutter}"
ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
BUILD_TOOLS="35.0.0"                             # the Android Gradle Plugin's default
SHIMS="$ANDROID_HOME/arm64-shims"
SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
PACKAGES=1 ONLY_SHIMS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-packages) PACKAGES=0; shift ;;
        --shims-only) ONLY_SHIMS=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
done
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
ARM64=0; [ "$(uname -m)" = aarch64 ] && ARM64=1

# --- ARM64: the x86-64 tools run under qemu-user ---------------------------------------------------
shims() {
    [ "$ARM64" = 1 ] || return 0
    command -v qemu-x86_64-static >/dev/null && [ -d /usr/x86_64-linux-gnu/lib ] ||
        die "qemu-user-static / libc6-amd64-cross missing - run scripts/setup_android_dev.sh"
    mkdir -p "$SHIMS"
    cat > "$SHIMS/x86run" <<'EOF'
#!/bin/sh
# Runs an x86-64 Linux program on ARM64 (setup_android_dev.sh): qemu-user, the x86-64 C library and
# the few other x86-64 libraries in x86-64-lib/. Options for qemu (-0 ARGV0) may come first.
LD_LIBRARY_PATH="$(dirname "$0")/x86-64-lib" exec qemu-x86_64-static -L /usr/x86_64-linux-gnu "$@"
EOF
    chmod +x "$SHIMS/x86run"

    # zlib for the NDK's tools: Debian's x86-64 package through a private apt state (apt checks the
    # archive's signatures; nothing changes on the system)
    if [ ! -e "$SHIMS/x86-64-lib/libz.so.1" ]; then
        local apt; apt=$(mktemp -d)
        mkdir -p "$apt/state/lists/partial" "$apt/cache/archives/partial" "$apt/dl" "$SHIMS/x86-64-lib"
        local A=(apt-get -q -o "Dir::State=$apt/state" -o "Dir::Cache=$apt/cache" -o APT::Architecture=amd64
                 -o APT::Architectures::=amd64 -o Debug::NoLocking=1)
        "${A[@]}" update >/dev/null 2>&1 || warn "apt update (x86-64 package lists) reported errors"
        (cd "$apt/dl" && "${A[@]}" download zlib1g:amd64 >/dev/null) || die "could not download zlib1g:amd64"
        dpkg-deb -x "$apt"/dl/zlib1g_*_amd64.deb "$apt/x"
        cp -P "$apt"/x/lib/x86_64-linux-gnu/libz.so.1* "$SHIMS/x86-64-lib/"
        rm -rf "$apt"
    fi

    # aapt2: the build Gradle would download for this project's Android Gradle Plugin, as x86-64
    local agp v
    agp=$(sed -n 's/.*"com.android.application") version "\([^"]*\)".*/\1/p' "$ROOT/app/android/settings.gradle.kts")
    [ -n "$agp" ] || die "no Android Gradle Plugin version in app/android/settings.gradle.kts"
    v=$(curl -fsSL https://dl.google.com/android/maven2/com/android/tools/build/aapt2/maven-metadata.xml |
        grep -o "<version>$agp-[0-9]*</version>" | sed 's/<[^>]*>//g' | tail -1)
    [ -n "$v" ] || die "no aapt2 for Android Gradle Plugin $agp"
    if [ ! -x "$SHIMS/aapt2-$v" ]; then
        local tmp; tmp=$(mktemp -d)
        curl -fsSL -o "$tmp/aapt2.jar" \
            "https://dl.google.com/android/maven2/com/android/tools/build/aapt2/$v/aapt2-$v-linux.jar"
        unzip -q -o "$tmp/aapt2.jar" aapt2 -d "$tmp"
        install -m 755 "$tmp/aapt2" "$SHIMS/aapt2-$v"
        rm -rf "$tmp"
    fi
    printf '#!/bin/sh\nexec "%s/x86run" "%s/aapt2-%s" "$@"\n' "$SHIMS" "$SHIMS" "$v" > "$SHIMS/aapt2"
    chmod +x "$SHIMS/aapt2"
    "$SHIMS/aapt2" version >/dev/null 2>&1 || die "aapt2 does not run under qemu ($SHIMS/aapt2)"
    echo "aapt2 $v (x86-64 under qemu): $SHIMS/aapt2"

    # gen_snapshot: Flutter downloads the linux-x64 one but runs <engine>/android-*/linux-arm64/
    local engine="$FLUTTER_DIR/bin/cache/artifacts/engine" d n=0
    ls -d "$engine"/android-*-release/linux-x64 >/dev/null 2>&1 || "$FLUTTER_DIR/bin/flutter" precache --android >/dev/null
    for d in "$engine"/android-*-profile "$engine"/android-*-release; do
        [ -x "$d/linux-x64/gen_snapshot" ] || continue
        mkdir -p "$d/linux-arm64"
        printf '#!/bin/sh\nexec "%s/x86run" "$(dirname "$0")/../linux-x64/gen_snapshot" "$@"\n' "$SHIMS" \
            > "$d/linux-arm64/gen_snapshot"
        chmod +x "$d/linux-arm64/gen_snapshot"
        n=$((n + 1))
    done
    [ "$n" -gt 0 ] || die "no gen_snapshot in $engine - run: flutter precache --android"
    "$engine/android-arm64-release/linux-arm64/gen_snapshot" --version >/dev/null 2>&1 ||
        die "gen_snapshot does not run under qemu"
    echo "gen_snapshot (x86-64 under qemu): $n wrappers in $engine/android-*/linux-arm64/"

    # NDK: the version Flutter's Gradle plugin asks for; only llvm-objcopy runs - as llvm-strip (argv[0]
    # decides) for the engine library, as itself for the debug symbol tables
    local ndk bin tool
    ndk=$(sed -n 's/.*val ndkVersion: String = "\([^"]*\)".*/\1/p' \
          "$FLUTTER_DIR/packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt" | head -1)
    [ -n "$ndk" ] || die "no ndkVersion in Flutter's Gradle plugin"
    if [ ! -d "$ANDROID_HOME/ndk/$ndk" ]; then
        echo "installing NDK $ndk (for its llvm-strip)"
        "$SDKMANAGER" --sdk_root="$ANDROID_HOME" "ndk;$ndk" >/dev/null || die "sdkmanager failed: ndk;$ndk"
    fi
    for bin in "$ANDROID_HOME"/ndk/*/toolchains/llvm/prebuilt/linux-x86_64/bin; do
        if [ -e "$bin/llvm-objcopy" ] && [ "$(head -c 4 "$bin/llvm-objcopy" | od -An -c | tr -d ' ')" = '177ELF' ]; then
            mv "$bin/llvm-objcopy" "$bin/llvm-objcopy.x86_64"
        fi
        [ -e "$bin/llvm-objcopy.x86_64" ] || continue
        for tool in llvm-objcopy llvm-strip; do
            rm -f "$bin/$tool"
            printf '#!/bin/sh\nexec "%s/x86run" -0 %s "$(dirname "$0")/llvm-objcopy.x86_64" "$@"\n' \
                "$SHIMS" "$tool" > "$bin/$tool"
            chmod +x "$bin/$tool"
        done
    done
    bin="$ANDROID_HOME/ndk/$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin"
    "$bin/llvm-strip" --version >/dev/null 2>&1 && "$bin/llvm-objcopy" --version >/dev/null 2>&1 ||
        die "the NDK's llvm-strip / llvm-objcopy do not run under qemu"
    echo "llvm-strip, llvm-objcopy of NDK $ndk (x86-64 under qemu)"

    gradle_props "android.aapt2FromMavenOverride=$SHIMS/aapt2"
}

# --- ~/.gradle/gradle.properties: this script's block, replaced on every run -----------------------
GRADLE_LINES=()
gradle_props() { GRADLE_LINES+=("$@"); }
write_gradle_props() {
    local f="${GRADLE_USER_HOME:-$HOME/.gradle}/gradle.properties" begin="# >>> setup_android_dev.sh >>>"
    local end="# <<< setup_android_dev.sh <<<"
    mkdir -p "$(dirname "$f")"; touch "$f"
    sed -i "/^$begin\$/,/^$end\$/d" "$f"
    [ ${#GRADLE_LINES[@]} -gt 0 ] || return 0
    { echo "$begin"; printf '%s\n' "${GRADLE_LINES[@]}"; echo "$end"; } >> "$f"
    echo "Gradle settings: $f"
}

if [ "$ONLY_SHIMS" = 1 ]; then
    shims
    # the Gradle block (with the memory caps) is written by a full run; the aapt2 path never changes
    f="${GRADLE_USER_HOME:-$HOME/.gradle}/gradle.properties"
    if [ "$ARM64" = 1 ] && ! grep -qxF "android.aapt2FromMavenOverride=$SHIMS/aapt2" "$f" 2>/dev/null; then
        die "Gradle does not use the aapt2 wrapper yet - run scripts/setup_android_dev.sh once"
    fi
    exit 0
fi

# --- packages -----------------------------------------------------------------------------------
step "Packages"
want=()
missing() { ! dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
java_ok() { command -v javac >/dev/null && [ "$(javac -version 2>&1 | sed -n 's/^javac \([0-9]*\).*/\1/p')" -ge 17 ]; }
java_ok || want+=(openjdk-17-jdk-headless)
for t in git curl unzip; do command -v $t >/dev/null || want+=($t); done
command -v adb >/dev/null || [ "$ARM64" = 0 ] || want+=(adb)       # x86-64: the SDK's platform-tools
if [ "$ARM64" = 1 ]; then
    for p in qemu-user-static libc6-amd64-cross libgcc-s1-amd64-cross; do missing $p && want+=($p); done
fi
if [ ${#want[@]} -eq 0 ]; then
    echo "nothing missing"
elif [ "$PACKAGES" = 1 ]; then
    command -v apt-get >/dev/null || die "install these yourself: ${want[*]}"
    echo "installing: ${want[*]}"
    sudo apt-get install -y --no-install-recommends "${want[@]}"
else
    die "missing: ${want[*]} (run without --no-packages, or install them)"
fi

# --- Flutter ------------------------------------------------------------------------------------
step "Flutter $FLUTTER_VERSION"
if [ ! -x "$FLUTTER_DIR/bin/flutter" ]; then
    git clone -q --depth 1 --branch "$FLUTTER_VERSION" https://github.com/flutter/flutter.git "$FLUTTER_DIR"
fi
have=$(git -C "$FLUTTER_DIR" describe --tags --exact-match 2>/dev/null || echo "?")
[ "$have" = "$FLUTTER_VERSION" ] || warn "$FLUTTER_DIR is Flutter $have, the project uses $FLUTTER_VERSION"
mkdir -p "$HOME/.local/bin"
ln -sf "$FLUTTER_DIR/bin/flutter" "$HOME/.local/bin/flutter"
ln -sf "$FLUTTER_DIR/bin/dart" "$HOME/.local/bin/dart"
FLUTTER="$FLUTTER_DIR/bin/flutter"
"$FLUTTER" --version 2>/dev/null | sed -n '/^Flutter /p'      # (head would cut its output: SIGPIPE)
"$FLUTTER" config --android-sdk "$ANDROID_HOME" >/dev/null
"$FLUTTER" precache --android >/dev/null && echo "engine artifacts: ok"

# --- Android SDK --------------------------------------------------------------------------------
step "Android SDK in $ANDROID_HOME"
# from 22.0 on, sdkmanager hands over to a native "android" program that is x86-64 only: ARM64 keeps
# 21.0, the last whose sdkmanager is all Java
[ "$ARM64" = 1 ] && [ -e "$ANDROID_HOME/cmdline-tools/latest/bin/android" ] && rm -rf "$ANDROID_HOME/cmdline-tools/latest"
if [ ! -x "$SDKMANAGER" ]; then
    if [ "$ARM64" = 1 ]; then
        zip=commandlinetools-linux-15641748_latest.zip
    else
        zip=$(curl -fsSL https://dl.google.com/android/repository/repository2-3.xml |
              grep -o 'commandlinetools-linux-[0-9]*_latest.zip' | sort -V | tail -1)
    fi
    [ -n "$zip" ] || die "no command-line tools found at dl.google.com"
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/$zip" "https://dl.google.com/android/repository/$zip"
    unzip -q "$tmp/$zip" -d "$tmp"
    mkdir -p "$ANDROID_HOME/cmdline-tools"
    rm -rf "$ANDROID_HOME/cmdline-tools/latest"
    mv "$tmp/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -rf "$tmp"
fi
echo "accepting the Android SDK licences"
{ yes || :; } | "$SDKMANAGER" --sdk_root="$ANDROID_HOME" --licenses >/dev/null || :
[ -f "$ANDROID_HOME/licenses/android-sdk-license" ] || die "the SDK licences were not accepted ($SDKMANAGER --licenses)"
api=$(sed -n 's/.*val compileSdkVersion: Int = \([0-9]*\).*/\1/p' \
      "$FLUTTER_DIR/packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt" | head -1)
pkgs=("platforms;android-${api:-36}" "build-tools;$BUILD_TOOLS")
[ "$ARM64" = 1 ] || pkgs+=("platform-tools")
"$SDKMANAGER" --sdk_root="$ANDROID_HOME" "${pkgs[@]}" >/dev/null ||
    die "sdkmanager failed: ${pkgs[*]}"
[ -f "$ANDROID_HOME/platforms/android-${api:-36}/android.jar" ] && [ -d "$ANDROID_HOME/build-tools/$BUILD_TOOLS" ] ||
    die "the SDK packages are not there after sdkmanager: ${pkgs[*]}"
echo "installed: ${pkgs[*]}"
if [ "$ARM64" = 1 ]; then
    # Google's platform-tools are x86-64 only: Flutter (and a shell, via ~/.local/bin) runs the system's
    # adb through this wrapper. A board that is an adb device itself has its adbd on the client's server
    # port 5037 and on 5555, where the server would find it as an "emulator": the developer's adb
    # server then uses 5038 and does not look for emulators (none runs on ARM64 Linux).
    sys_adb=$(PATH=/usr/local/bin:/usr/bin:/bin command -v adb) || die "adb missing"
    port=""
    if pgrep -x adbd >/dev/null && ss -ltn 2>/dev/null | grep -q '127.0.0.1:5037 '; then port=5038; fi
    mkdir -p "$ANDROID_HOME/platform-tools"
    [ -x "$ANDROID_HOME/platform-tools/adb" ] && "$ANDROID_HOME/platform-tools/adb" kill-server 2>/dev/null
    rm -f "$ANDROID_HOME/platform-tools/adb"
    {
        echo '#!/bin/sh'
        echo '# setup_android_dev.sh: the system adb (Google publishes platform-tools for x86-64 only)'
        [ -z "$port" ] || printf '%s\n' ': "${ANDROID_ADB_SERVER_PORT:='"$port"'}"   # this board'"'"'s adbd holds 5037' \
            ': "${ADB_EMU:=0}"                    # ... and 5555: not an emulator' 'export ANDROID_ADB_SERVER_PORT ADB_EMU'
        echo "exec $sys_adb \"\$@\""
    } > "$ANDROID_HOME/platform-tools/adb"
    chmod +x "$ANDROID_HOME/platform-tools/adb"
    ln -sf "$ANDROID_HOME/platform-tools/adb" "$HOME/.local/bin/adb"
    "$ANDROID_HOME/platform-tools/adb" devices >/dev/null 2>&1 || die "adb does not start its server"
    echo "adb: $sys_adb${port:+ (server on port $port)}"
fi

# --- ARM64 shims, Gradle memory -----------------------------------------------------------------
if [ "$ARM64" = 1 ]; then step "x86-64 tools under qemu"; fi
shims
mem_gb=$(awk '/MemTotal/{print int($2/1048576 + 0.5)}' /proc/meminfo)
if [ "$mem_gb" -lt 12 ]; then
    # the project asks for an 8 GB heap; a board with 8 GB also runs the server. Kotlin compiles inside
    # Gradle's process (no daemon of its own), and an idle Gradle daemon gives its memory back after 15 min
    gradle_props "org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g" \
        "kotlin.compiler.execution.strategy=in-process" "org.gradle.daemon.idletimeout=900000"
fi
write_gradle_props

step "Check"
"$FLUTTER" doctor 2>&1 | grep -E "^\[|✗|!" | grep -iv "chrome\|linux toolchain\|visual studio\|clang\|ninja\|on your path" || true
cat <<EOF

Done. Build:  scripts/build_apk.sh        (new shells find flutter in ~/.local/bin)
Release key:  copy app/android/key.properties + its keystore here, or a new key is created.
EOF
