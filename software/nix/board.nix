# Manafish mainboard (Compute Module 5 with eMMC): the board settings and the
# local additions upstream's modules don't have. Keeping them here keeps the
# patches to upstream's files small for `git subtree pull`. The same image also
# boots on the official CM5 IO Board, which uses the firmware's default
# bcm2712-rpi-cm5-cm5io.dtb. Nothing here depends on wireless, the RAM size or
# the eMMC size.
{
  lib,
  pkgs,
  ...
}: {
  hardware.raspberry-pi = {
    # nixos-raspberrypi renders `false` as `key=`, so use 0/1.
    config.all.options = {
      # The cameras are listed below. There is no DSI display and no HAT
      # EEPROM: GPIO0/1 are camera 2's I2C.
      camera_auto_detect.value = 0;
      display_auto_detect.value = 0;
      force_eeprom_read = {
        enable = true;
        value = 0;
      };
      # GPIO14/15 are camera 2's IO0/IO1, not UART0. The base sets 1 without
      # mkDefault.
      enable_uart.value = lib.mkForce 0;
    };

    # Without the [all] header this would land in the base's [cm5] section.
    # dt-overlays is keyed by name, so the two imx477 lines live here.
    # GPIO14 has to be driven here: on the CM5 the dtb's cam1_reg is an alias
    # of cam0_reg (CAM_GPIO), so the imx477 overlay never switches GPIO14.
    extra-config = ''
      [all]
      # PoE type detect TYP2/TYP3, active low
      gpio=6,12=ip,pu
      # RP2354A RUN and QSPI_SS: released, external pull-ups
      gpio=24,25=ip,pn
      # Camera 2 (J11) power enable
      gpio=14=op,dh
      # Camera 1 on CAM/DISP0 (J5), camera 2 on CAM/DISP1 (J11)
      dtoverlay=imx477,cam0
      dtoverlay=imx477
    '';
  };

  # rpicam-vid encodes H.264 in software inside go2rtc's cgroup. Under CPU
  # contention the MCU link and the command stream in manafish-firmware get
  # four times go2rtc's share (other services: 100). A share, not a priority.
  systemd.services = {
    go2rtc.serviceConfig.CPUWeight = 50;
    manafish-firmware.serviceConfig.CPUWeight = 200;
  };

  # Let `pi` (the firmware service and SSH sessions) power off and reboot.
  security.polkit = {
    enable = true;
    extraConfig = ''
      polkit.addRule(function (action, subject) {
        if (
          subject.user == "pi" &&
          /^org\.freedesktop\.login1\.(power-off|reboot)(-multiple-sessions)?$/.test(action.id)
        ) {
          return polkit.Result.YES;
        }
      });
    '';
  };

  # Bring-up tools. The nixos-raspberrypi base already ships raspberrypi-utils
  # (pinctrl, vcgencmd), the gpio group and its udev rules; the sd-image base
  # profile ships usbutils (lsusb).
  environment.systemPackages = with pkgs; [
    libgpiod
    ethtool
    iperf3
  ];

  # No device on the CM5 or the mainboard loads a linux-firmware blob: the
  # radios are disabled (networking.nix), and the Ethernet MAC, RP1, the
  # imx477 cameras and cdc_acm need none. The base turns it on without
  # mkDefault; dropping it roughly halves the image.
  hardware.enableRedistributableFirmware = lib.mkForce false;

  # Allow `nixos-rebuild --flake` on the device and nix copies from wheel users.
  nix.settings = {
    experimental-features = ["nix-command" "flakes"];
    trusted-users = ["@wheel"];
  };
}
