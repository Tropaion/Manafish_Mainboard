{pkgs, ...}: {
  # Expose the MCU's USB CDC serial interface as /dev/ttyACM*.
  boot.kernelModules = ["cdc_acm"];

  # picotool's rules give plugdev the RP-series USB devices (BOOTSEL and
  # CDC). pi is not in dialout, so their serial ports need their own rule,
  # which also covers a development Pico (2e8a:000a). The mainboard MCU
  # (2e8a:0009) also gets a stable name.
  services.udev.packages = [pkgs.picotool];
  services.udev.extraRules = ''
    SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="2e8a", MODE="660", GROUP="plugdev", TAG+="uaccess"
    SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0009", SYMLINK+="manafish-mcu"
  '';

  # The MCU firmware is a release asset (see flake.nix); only flash it here.
  environment.systemPackages = [pkgs.picotool];
}
