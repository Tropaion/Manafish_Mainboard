{
  pkgs,
  lib,
  inputs,
  mcuFirmwareVersion,
  escFirmwareVersion,
  ...
}: let
  username = "pi";
  homeDir = "/home/${username}";
  firmwareSource = ./..;
  cmp = lib.getExe' pkgs.diffutils "cmp";
in {
  # The SD image uses ext4 and vfat. The base profile's unused ZFS support
  # can force a full kernel rebuild when its module is missing from caches.
  boot.supportedFilesystems.zfs = false;

  nixpkgs.overlays = [
    (_: prev: {
      libadwaita = prev.libadwaita.overrideAttrs (_: {
        doCheck = false;
      });
    })
  ];

  users.users.${username} = {
    isNormalUser = true;
    extraGroups = ["wheel" "networkmanager" "video" "i2c" "gpio" "plugdev"];
    password = "manafish";
    home = homeDir;
  };

  environment.systemPackages = with pkgs; [
    neovim
    nano
    helix
  ];

  # Deploy firmware files and MCU firmware to the user's home directory
  systemd.services.manafish-setup = {
    wantedBy = ["multi-user.target"];
    before = ["manafish-firmware.service"];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = username;
    };
    script = ''
      FIRMWARE_DIR="${homeDir}/firmware"
      MCU_DIR="${homeDir}/mcu-firmware"
      ESC_FIRMWARE_DIR="${homeDir}/esc-firmware"
      CONFIG="$FIRMWARE_DIR/src/rov_firmware/config.json"
      MARKER="$FIRMWARE_DIR/.nix-source"
      CURRENT_SOURCE="${firmwareSource}"

      if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER")" != "$CURRENT_SOURCE" ]; then
        CONFIG_BACKUP="${homeDir}/.config-backup.json"
        [ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG_BACKUP"
        # The magnet switch cuts power without warning: flush each copy before
        # the step that relies on it.
        sync
        rm -rf "$FIRMWARE_DIR"
        cp -r "$CURRENT_SOURCE" "$FIRMWARE_DIR"
        chmod -R u+w "$FIRMWARE_DIR"
        [ -f "$CONFIG_BACKUP" ] && mv "$CONFIG_BACKUP" "$CONFIG"
        # The marker goes last: a cut before it lands only repeats the deploy.
        sync
        echo "$CURRENT_SOURCE" > "$MARKER"
        sync
      fi

      # deploy SRC DIR NAME STALE: copy SRC to DIR/NAME and delete the other
      # files matching STALE. Compare content, not existence: a power cut soon
      # after a copy can leave an empty file behind. install replaces a
      # read-only target.
      deploy() {
        mkdir -p "$2"
        find "$2" -maxdepth 1 -type f -name "$4" ! -name "$3" -delete
        if ! ${cmp} -s "$1" "$2/$3"; then
          install -m 644 "$1" "$2/$3"
          sync "$2/$3"
        fi
      }
      deploy ${inputs.mcu-firmware} "$MCU_DIR" "mainboard-${mcuFirmwareVersion}.uf2" "mainboard-v*.uf2"
      deploy ${inputs.esc-firmware} "$ESC_FIRMWARE_DIR" "esc-${escFirmwareVersion}.bin" "esc-v*"
    '';
  };
}
