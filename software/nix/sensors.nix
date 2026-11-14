{pkgs, ...}: {
  hardware = {
    i2c.enable = true;
    raspberry-pi.config.all.base-dt-params = {
      i2c_arm = {
        enable = true;
        value = "on";
      };
      # The mainboard's LTC4162 and WSEN-PADS allow at most 400 kHz.
      i2c_arm_baudrate = {
        enable = true;
        value = 400000;
      };
    };
  };

  # Expose /dev/i2c-* devices for I2C sensors.
  boot.kernelModules = ["i2c-dev"];

  environment.systemPackages = with pkgs; [
    i2c-tools
  ];
}
