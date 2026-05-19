#!/usr/bin/env bash
# ============================================================================
# hermes-skill-deps.sh — Production dependency installer for Hermes Agent skills
#
# Supports: macOS (Homebrew), Linux (apt/dnf/pacman/apk), Windows (WSL/MSYS2)
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/.../hermes-skill-deps.sh | bash
#   bash hermes-skill-deps.sh              # install all
#   bash hermes-skill-deps.sh --list       # list categories
#   bash hermes-skill-deps.sh --category ocr
#   bash hermes-skill-deps.sh --dry-run    # preview only
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

run() {
  if $DRY_RUN; then
    echo "  [DRY-RUN] $*"
  else
    "$@"
  fi
}

# ── OS Detection ────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s 2>/dev/null)" in
    Darwin*)  OS="macos";;
    Linux*)   OS="linux";;
    MINGW*|MSYS*|CYGWIN*) OS="windows";;
    *)        OS="unknown";;
  esac
  # Detect Linux distro
  if [[ "$OS" == "linux" ]]; then
    if command -v apt-get &>/dev/null; then DISTRO="debian"
    elif command -v dnf &>/dev/null; then DISTRO="fedora"
    elif command -v pacman &>/dev/null; then DISTRO="arch"
    elif command -v apk &>/dev/null; then DISTRO="alpine"
    else DISTRO="unknown"; fi
  fi
  info "Platform: ${OS}${DISTRO:+ ($DISTRO)}"
}

# ── Helpers ─────────────────────────────────────────────────────────────────
check_cmd() { command -v "$1" &>/dev/null; }

install_system_pkg() {
  # Cross-platform system package install
  local pkg_mac="$1" pkg_deb="$2" pkg_fedora="$3" pkg_arch="$4" pkg_alpine="$5"
  case "$OS" in
    macos)   run brew install "$pkg_mac";;
    linux)
      case "$DISTRO" in
        debian) run sudo apt-get install -y -qq "$pkg_deb";;
        fedora) run sudo dnf install -y -q "$pkg_fedora";;
        arch)   run sudo pacman -S --noconfirm "$pkg_arch";;
        alpine) run sudo apk add --no-cache "$pkg_alpine";;
        *)      fail "Unsupported distro for auto-install"; return 1;;
      esac
      ;;
    windows)
      if check_cmd choco; then run choco install -y "$pkg_mac"
      elif check_cmd scoop; then run scoop install "$pkg_mac"
      elif check_cmd winget; then run winget install --id "$pkg_mac" --accept-package-agreements
      else fail "No package manager (install choco/scoop/winget)"; return 1
      fi
      ;;
  esac
}

pip_install() {
  local PIP
  PIP=$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null || true)
  if [[ -z "$PIP" ]]; then
    warn "pip not found — skipping: $*"
    return 1
  fi
  run "$PIP" install --quiet --upgrade "$@"
}

npm_install() {
  if ! check_cmd npm; then
    warn "npm not found — skipping: $*"
    return 1
  fi
  run npm install -g "$@"
}

# ── Category: ocr ───────────────────────────────────────────────────────────
cat_ocr() {
  info "=== OCR (Tesseract + Poppler) ==="
  if check_cmd tesseract && check_cmd pdftoppm; then
    ok "tesseract + poppler already installed"; return 0
  fi
  case "$OS" in
    macos)
      run brew install tesseract tesseract-lang poppler
      ;;
    linux)
      case "$DISTRO" in
        debian)
          run sudo apt-get update -qq
          run sudo apt-get install -y -qq tesseract-ocr tesseract-ocr-eng \
              tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn \
              tesseract-ocr-kor poppler-utils
          ;;
        fedora)
          run sudo dnf install -y -q tesseract tesseract-langpack-eng \
              tesseract-langpack-chi_sim tesseract-langpack-jpn poppler-utils
          ;;
        arch)
          run sudo pacman -S --noconfirm tesseract tesseract-data-eng \
              tesseract-data-chi_sim tesseract-data-jpn poppler
          ;;
        alpine)
          run sudo apk add --no-cache tesseract-ocr tesseract-ocr-data-eng \
              tesseract-ocr-data-chi_sim poppler-utils
          ;;
      esac
      ;;
    windows)
      warn "Windows: download Tesseract from https://github.com/UB-Mannheim/tesseract/wiki"
      warn "Add install dir to PATH. Poppler: https://github.com/oschwartz10612/poppler-windows"
      ;;
  esac
  pip_install pytesseract Pillow pymupdf
  ok "OCR stack ready"
}

# ── Category: pandoc ────────────────────────────────────────────────────────
cat_pandoc() {
  info "=== Pandoc (Document Conversion) ==="
  if check_cmd pandoc; then ok "pandoc $(pandoc --version | head -1) already installed"; return 0; fi
  install_system_pkg pandoc pandoc pandoc pandoc pandoc
  info "Optional: for PDF output install a TeX engine"
  case "$OS" in
    macos)   info "  brew install --cask mactex-no-gui";;
    linux)   info "  apt install texlive-xetex texlive-fonts-recommended texlive-fonts-extra";;
    windows) info "  choco install miktex";;
  esac
  ok "Pandoc installed"
}

# ── Category: git ───────────────────────────────────────────────────────────
cat_git() {
  info "=== Git + GitHub CLI ==="
  if ! check_cmd git; then
    install_system_pkg git git git git git
  fi
  ok "git $(git --version 2>/dev/null | awk '{print $3}')"
  if check_cmd gh; then
    ok "gh $(gh --version 2>/dev/null | head -1 | awk '{print $3}')"
    return 0
  fi
  info "Installing GitHub CLI..."
  case "$OS" in
    macos)   run brew install gh;;
    linux)
      case "$DISTRO" in
        debian)
          run sudo mkdir -p -m 755 /etc/apt/keyrings
          wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | run sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
          echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            | run sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
          run sudo apt-get update -qq && run sudo apt-get install -y -qq gh
          ;;
        fedora) run sudo dnf install -y -q gh;;
        arch)   run sudo pacman -S --noconfirm github-cli;;
        alpine) run sudo apk add --no-cache github-cli;;
      esac
      ;;
    windows)
      if check_cmd winget; then run winget install --id GitHub.cli --accept-package-agreements
      elif check_cmd choco; then run choco install -y gh
      fi
      ;;
  esac
  ok "GitHub CLI installed"
}

# ── Category: media ─────────────────────────────────────────────────────────
cat_media() {
  info "=== Media (ffmpeg + ImageMagick) ==="
  if ! check_cmd ffmpeg; then
    install_system_pkg ffmpeg ffmpeg ffmpeg ffmpeg ffmpeg
  fi
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}' || echo 'installed')"
  install_system_pkg imagemagick imagemagick ImageMagick imagemagick imagemagick
  ok "ImageMagick installed"
}

# ── Category: python ────────────────────────────────────────────────────────
cat_python() {
  info "=== Python Packages ==="
  if ! check_cmd python3 && ! check_cmd python; then
    fail "Python 3.10+ required. Install from https://python.org"
    return 1
  fi
  pip_install \
    pytesseract Pillow yfinance pymupdf \
    beautifulsoup4 lxml httpx \
    pandas matplotlib
  ok "Python packages installed"
}

# ── Category: node ──────────────────────────────────────────────────────────
cat_node() {
  info "=== Node.js ==="
  if check_cmd node && check_cmd npx; then
    ok "node $(node --version) already installed"; return 0
  fi
  case "$OS" in
    macos)   run brew install node;;
    linux)
      case "$DISTRO" in
        debian)
          curl -fsSL https://deb.nodesource.com/setup_22.x | run sudo -E bash -
          run sudo apt-get install -y -qq nodejs
          ;;
        fedora) run sudo dnf install -y -q nodejs;;
        arch)   run sudo pacman -S --noconfirm nodejs npm;;
        alpine) run sudo apk add --no-cache nodejs npm;;
      esac
      ;;
    windows)
      if check_cmd choco; then run choco install -y nodejs
      elif check_cmd winget; then run winget install --id OpenJS.NodeJS.LTS --accept-package-agreements
      fi
      ;;
  esac
  ok "Node.js $(node --version 2>/dev/null || echo 'installed')"
}

# ── Category: lsp ───────────────────────────────────────────────────────────
cat_lsp() {
  info "=== LSP Language Servers ==="
  if check_cmd npm; then
    npm_install pyright typescript typescript-language-server 2>/dev/null || true
  fi
  if check_cmd rustup; then
    run rustup component add rust-analyzer 2>/dev/null || ok "rust-analyzer already available"
  fi
  if check_cmd go; then
    run go install golang.org/x/tools/gopls@latest 2>/dev/null || true
  fi
  ok "LSP servers installed (available languages)"
}

# ── Category: notify ────────────────────────────────────────────────────────
cat_notify() {
  info "=== Desktop Notifications ==="
  case "$OS" in
    macos)   run brew install terminal-notifier 2>/dev/null || ok "osascript (built-in) available";;
    linux)   install_system_pkg libnotify-bin libnotify libnotify libnotify libnotify;;
    windows)
      if check_cmd powershell; then
        run powershell -Command "Install-Module -Name BurntToast -Force -Scope CurrentUser" 2>/dev/null || true
      fi
      ;;
  esac
  ok "Notification tools available"
}

# ── Category: mcp ───────────────────────────────────────────────────────────
cat_mcp() {
  info "=== MCP Runtime (Python mcp package) ==="
  pip_install mcp "mcp[cli]"
  ok "MCP SDK installed"
}

# ── Main ────────────────────────────────────────────────────────────────────
ALL_CATEGORIES="ocr pandoc git media python node lsp notify mcp"

install_all() {
  local failed=0
  for cat in $ALL_CATEGORIES; do
    "cat_${cat}" || ((failed++))
    echo ""
  done
  if [[ $failed -gt 0 ]]; then
    warn "$failed category(ies) had issues — check output above"
  fi
}

install_category() {
  local name="$1"
  if ! type "cat_${name}" &>/dev/null; then
    fail "Unknown category: ${name}"
    fail "Available: ${ALL_CATEGORIES}"
    exit 1
  fi
  "cat_${name}"
}

# ── Entry ───────────────────────────────────────────────────────────────────
detect_os

case "${1:---all}" in
  --all)       info "Installing ALL skill dependencies..."; echo ""; install_all;;
  --category)  install_category "${2:?Usage: $0 --category NAME}";;
  --list)
    echo "Categories:"
    echo "  ocr      → tesseract, poppler, pytesseract, pymupdf"
    echo "  pandoc   → pandoc (+ optional TeX for PDF)"
    echo "  git      → git, gh (GitHub CLI)"
    echo "  media    → ffmpeg, ImageMagick"
    echo "  python   → pytesseract, Pillow, yfinance, pymupdf, beautifulsoup4, httpx, pandas"
    echo "  node     → Node.js 22 LTS, npm, npx"
    echo "  lsp      → pyright, typescript-language-server, rust-analyzer, gopls"
    echo "  notify   → terminal-notifier (macOS), libnotify (Linux), BurntToast (Windows)"
    echo "  mcp      → Python MCP SDK"
    ;;
  --dry-run)   DRY_RUN=true; info "DRY RUN — showing what would be installed"; echo ""; install_all;;
  --help|-h)
    echo "Usage: $0 [OPTION]"
    echo "  --all              Install all dependencies (default)"
    echo "  --category CAT     Install a specific category"
    echo "  --list             List categories and what they install"
    echo "  --dry-run          Preview without installing"
    echo "  --help             Show this help"
    ;;
  *)  fail "Unknown option: $1"; exit 1;;
esac

echo ""
ok "Done. Skills requiring these tools will now work."
