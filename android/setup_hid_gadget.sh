#!/system/bin/sh
# =============================================================================
# DevRig-HID :: android/setup_hid_gadget.sh
#
# Adds a standard USB boot-keyboard HID function (functions/hid.usb0) to the
# Linux ConfigFS gadget /sys/kernel/config/usb_gadget/g1 so the board enumerates
# as a composite device: ADB bridge + HID keyboard (/dev/hidg0).
#
#   * If the Android-managed gadget "g1" already exists (normal case) the HID
#     function is added to its active configuration (ADB stays untouched).
#   * If it does not exist, a complete composite gadget (ffs.adb + hid.usb0)
#     is created from scratch.
#
# Usage (as root):
#   sh setup_hid_gadget.sh [--teardown] [--status] [--foreground]
#                          [--udc NAME] [--permissive] [--help]
#
# Re-binding the UDC drops the USB link for a few seconds, which kills any
# `adb shell` session that started this script.  Therefore the script detaches
# itself into the background and writes its outcome to
#   /data/local/tmp/devrig/gadget_setup.log
# The last line is always  DEVRIG_RESULT=OK ...  or  DEVRIG_RESULT=FAIL ...
#
# Environment overrides: GADGET_NAME (g1) DEVRIG_VID (0x18d1) DEVRIG_PID (0x4ee7)
#                        DEVRIG_SERIAL
# =============================================================================

WORKDIR=/data/local/tmp/devrig
LOG=$WORKDIR/gadget_setup.log
CREATED_MARK=$WORKDIR/gadget_created_by_devrig
ORIG_UDC_FILE=$WORKDIR/original_udc
CONFIGFS=/sys/kernel/config
GADGET_NAME="${GADGET_NAME:-g1}"
G=$CONFIGFS/usb_gadget/$GADGET_NAME
FUNC=hid.usb0
F=$G/functions/$FUNC
VID="${DEVRIG_VID:-0x18d1}"
PID="${DEVRIG_PID:-0x4ee7}"
SERIAL="${DEVRIG_SERIAL:-DEVRIG0001}"

MODE=setup
FOREGROUND=0
PERMISSIVE=0
UDC_ARG=""

# Standard 8-byte boot keyboard report descriptor (63 bytes), octal escapes.
DESC_OCTAL='\005\001\011\006\241\001\005\007\031\340\051\347\025\000\045\001\165\001\225\010\201\002\225\001\165\010\201\003\225\005\165\001\005\010\031\001\051\005\221\002\225\001\165\003\221\003\225\006\165\010\025\000\045\145\005\007\031\000\051\145\201\000\300'
DESC_B64='BQEJBqEBBQcZ4CnnFQAlAXUBlQiBApUBdQiBA5UFdQEFCBkBKQWRApUBdQORA5UGdQgVACVlBQcZACllgQDA'
DESC_LEN=63

log()  { echo "[$(date '+%H:%M:%S' 2>/dev/null)] $*"; }
fail() { log "ERROR: $*"; echo "DEVRIG_RESULT=FAIL reason=$*"; exit 1; }

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --teardown)   MODE=teardown ;;
        --status)     MODE=status; FOREGROUND=1 ;;
        --foreground) FOREGROUND=1 ;;
        --permissive) PERMISSIVE=1 ;;
        --udc)        shift; UDC_ARG="$1" ;;
        -h|--help)    usage ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# ---- detach so that the USB re-enumeration cannot kill us -------------------
if [ "${DEVRIG_CHILD:-0}" != "1" ] && [ "$FOREGROUND" != "1" ]; then
    mkdir -p "$WORKDIR" 2>/dev/null
    : > "$LOG"
    DEVRIG_CHILD=1 nohup sh "$0" "$@" >> "$LOG" 2>&1 < /dev/null &
    echo "detached (pid $!); progress in $LOG"
    exit 0
fi
trap '' HUP

mkdir -p "$WORKDIR" 2>/dev/null

[ "$(id -u)" = "0" ] || fail "must run as root (su)"

# ---- helpers ------------------------------------------------------------------
write_attr() {          # write_attr <value> <file>
    echo "$1" > "$2" 2>/dev/null || fail "cannot write '$1' to $2"
}

write_descriptor() {    # write the 63-byte descriptor, trying several encoders
    target="$1"
    for attempt in printf_bin printf_sh echo_ne base64; do
        case "$attempt" in
            printf_bin) [ -x /system/bin/printf ] && /system/bin/printf "$DESC_OCTAL" > "$target" 2>/dev/null ;;
            printf_sh)  printf "$DESC_OCTAL" > "$target" 2>/dev/null ;;
            echo_ne)    echo -ne "$DESC_OCTAL" > "$target" 2>/dev/null ;;
            base64)     echo "$DESC_B64" | base64 -d > "$target" 2>/dev/null ;;
        esac
        size=$(wc -c < "$target" 2>/dev/null)
        size=$(echo $size)
        if [ "$size" = "$DESC_LEN" ]; then
            log "report descriptor written via $attempt ($size bytes)"
            return 0
        fi
        log "descriptor encoder '$attempt' produced '$size' bytes, trying next"
    done
    return 1
}

current_udc() { cat "$G/UDC" 2>/dev/null | tr -d '\n '; }

pick_udc() {
    if [ -n "$UDC_ARG" ]; then echo "$UDC_ARG"; return; fi
    cur=$(current_udc)
    if [ -n "$cur" ]; then echo "$cur"; return; fi
    if [ -s "$ORIG_UDC_FILE" ]; then cat "$ORIG_UDC_FILE"; return; fi
    ls /sys/class/udc 2>/dev/null | head -n 1
}

unbind_udc() {
    cur=$(current_udc)
    [ -z "$cur" ] && return 0
    echo "$cur" > "$ORIG_UDC_FILE"
    echo "" > "$G/UDC" 2>/dev/null
    [ -n "$(current_udc)" ] && echo "none" > "$G/UDC" 2>/dev/null
    [ -z "$(current_udc)" ] || fail "could not unbind UDC '$cur'"
    log "UDC '$cur' unbound"
    sleep 1
}

bind_udc() {
    udc="$1"
    [ -n "$udc" ] || fail "no UDC controller found under /sys/class/udc"
    i=0
    while [ $i -lt 5 ]; do
        if echo "$udc" > "$G/UDC" 2>/dev/null && [ "$(current_udc)" = "$udc" ]; then
            log "gadget bound to UDC '$udc'"
            return 0
        fi
        i=$((i + 1)); sleep 1
    done
    return 1
}

first_config() { ls -d "$G"/configs/* 2>/dev/null | head -n 1; }

wait_for_node() {
    i=0
    while [ $i -lt 50 ]; do
        ls /dev/hidg* >/dev/null 2>&1 && return 0
        i=$((i + 1)); sleep 0.2 2>/dev/null || sleep 1
    done
    return 1
}

fix_permissions() {
    for n in /dev/hidg*; do
        [ -e "$n" ] && chmod 666 "$n" && log "chmod 666 $n"
    done
    if [ "$PERMISSIVE" = "1" ] && command -v setenforce >/dev/null 2>&1; then
        setenforce 0 && log "SELinux set to permissive"
    fi
}

node_name() { ls /dev/hidg* 2>/dev/null | head -n 1; }

# ---- STATUS -------------------------------------------------------------------
if [ "$MODE" = "status" ]; then
    echo "configfs mounted : $(grep -c configfs /proc/mounts)"
    echo "gadget dir       : $( [ -d "$G" ] && echo "$G present" || echo "$G missing")"
    echo "bound UDC        : $(current_udc)"
    for s in /sys/class/udc/*/state; do [ -e "$s" ] && echo "udc state        : $(cat "$s") ($s)"; done
    echo "hid function     : $( [ -d "$F" ] && echo present || echo missing)"
    echo "hid nodes        : $(ls /dev/hidg* 2>/dev/null | tr '\n' ' ')"
    echo "configs          : $(ls "$G/configs" 2>/dev/null | tr '\n' ' ')"
    exit 0
fi

# ---- common prerequisites -----------------------------------------------------
grep -q configfs /proc/mounts 2>/dev/null || mount -t configfs none "$CONFIGFS" 2>/dev/null \
    || fail "cannot mount configfs (kernel lacks CONFIG_CONFIGFS_FS)"
[ -d "$CONFIGFS/usb_gadget" ] || fail "kernel lacks CONFIG_USB_CONFIGFS (no usb_gadget directory)"

# ---- TEARDOWN -----------------------------------------------------------------
if [ "$MODE" = "teardown" ]; then
    [ -d "$G" ] || { log "gadget $G does not exist; nothing to do"; echo "DEVRIG_RESULT=OK teardown"; exit 0; }
    UDC_NAME=$(pick_udc)
    unbind_udc
    for cfg in "$G"/configs/*; do
        [ -e "$cfg/$FUNC" ] && rm "$cfg/$FUNC" && log "unlinked $cfg/$FUNC"
    done
    if [ -d "$F" ]; then rmdir "$F" 2>/dev/null && log "removed $FUNC" || log "WARN: could not remove $F"; fi
    if [ -f "$CREATED_MARK" ]; then
        log "gadget was created by DevRig-HID; removing it completely"
        for cfg in "$G"/configs/*; do
            [ -d "$cfg" ] || continue
            for l in "$cfg"/*; do [ -L "$l" ] && rm "$l"; done
            rmdir "$cfg"/strings/* 2>/dev/null
            rmdir "$cfg" 2>/dev/null
        done
        for fn in "$G"/functions/*; do [ -d "$fn" ] && rmdir "$fn" 2>/dev/null; done
        rmdir "$G"/strings/* 2>/dev/null
        rmdir "$G" 2>/dev/null || log "WARN: could not remove $G"
        rm -f "$CREATED_MARK"
    else
        bind_udc "$UDC_NAME" || fail "could not rebind UDC '$UDC_NAME' after teardown"
    fi
    echo "DEVRIG_RESULT=OK teardown"
    exit 0
fi

# ---- SETUP --------------------------------------------------------------------
UDC_NAME=$(pick_udc)
[ -n "$UDC_NAME" ] || fail "no UDC controller found under /sys/class/udc"
log "target UDC: $UDC_NAME"

# idempotent fast path: already configured and enumerated
if [ -d "$F" ] && [ "$(current_udc)" = "$UDC_NAME" ]; then
    cfg=$(first_config)
    if [ -n "$cfg" ] && [ -e "$cfg/$FUNC" ] && ls /dev/hidg* >/dev/null 2>&1; then
        fix_permissions
        log "HID gadget already active; nothing to change"
        echo "DEVRIG_RESULT=OK node=$(node_name) mode=already-active"
        exit 0
    fi
fi

unbind_udc

if [ ! -d "$G" ]; then
    log "gadget $GADGET_NAME not found: creating a composite ADB+HID gadget"
    mkdir -p "$G" || fail "cannot create $G"
    : > "$CREATED_MARK"
    write_attr "$VID" "$G/idVendor"
    write_attr "$PID" "$G/idProduct"
    write_attr 0x0200 "$G/bcdUSB"
    write_attr 0x0100 "$G/bcdDevice"
    mkdir -p "$G/strings/0x409" || fail "cannot create gadget strings"
    write_attr "$SERIAL" "$G/strings/0x409/serialnumber"
    write_attr "DevRig" "$G/strings/0x409/manufacturer"
    write_attr "DevRig-HID Rig" "$G/strings/0x409/product"
    mkdir -p "$G/configs/b.1/strings/0x409" || fail "cannot create config b.1"
    write_attr "ADB + HID Keyboard" "$G/configs/b.1/strings/0x409/configuration"
    write_attr 0x80 "$G/configs/b.1/bmAttributes"
    write_attr 500 "$G/configs/b.1/MaxPower"
    # FunctionFS ADB endpoint (adbd must serve it)
    if mkdir -p "$G/functions/ffs.adb" 2>/dev/null; then
        mkdir -p /dev/usb-ffs/adb
        grep -q '/dev/usb-ffs/adb' /proc/mounts || mount -o uid=2000,gid=1000 -t functionfs adb /dev/usb-ffs/adb 2>/dev/null \
            || log "WARN: could not mount functionfs for adb"
        ln -s "$G/functions/ffs.adb" "$G/configs/b.1/ffs.adb" 2>/dev/null
        setprop ctl.restart adbd 2>/dev/null
        i=0
        while [ $i -lt 20 ] && [ ! -e /dev/usb-ffs/adb/ep1 ]; do i=$((i + 1)); sleep 1; done
        [ -e /dev/usb-ffs/adb/ep1 ] || log "WARN: adbd did not populate FunctionFS endpoints; continuing"
    else
        log "WARN: kernel has no ffs function; ADB will not be available over USB"
    fi
fi

CFG=$(first_config)
[ -n "$CFG" ] || fail "gadget $G has no configuration directory"
log "using configuration $CFG"

mkdir -p "$F" 2>/dev/null || fail "kernel lacks the HID gadget function (CONFIG_USB_CONFIGFS_F_HID)"
write_attr 1 "$F/protocol"          # keyboard
write_attr 1 "$F/subclass"          # boot interface
write_attr 8 "$F/report_length"
write_descriptor "$F/report_desc" || fail "could not write the HID report descriptor"
[ -e "$CFG/$FUNC" ] || ln -s "$F" "$CFG/$FUNC" || fail "cannot link $FUNC into $CFG"
log "linked $FUNC into $(basename "$CFG")"

if ! bind_udc "$UDC_NAME"; then
    # most common reason: FunctionFS ADB not ready -> retry as HID-only
    log "WARN: bind failed; retrying without ffs.adb"
    unbind_udc
    for l in "$CFG"/ffs.*; do [ -L "$l" ] && rm "$l" && log "temporarily unlinked $l"; done
    bind_udc "$UDC_NAME" || fail "UDC bind failed ('$UDC_NAME')"
fi

wait_for_node || fail "/dev/hidg* did not appear after binding"
fix_permissions
log "gadget ready: $(node_name)"
echo "DEVRIG_RESULT=OK node=$(node_name) mode=configured"
exit 0
