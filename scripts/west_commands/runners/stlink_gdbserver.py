# Copyright (c) 2025 STMicroelectronics
#
# SPDX-License-Identifier: Apache-2.0

"""
Runner for debugging applications using the ST-LINK GDB server
from STMicroelectronics, provided as part of the STM32CubeCLT.
"""

import argparse
import platform
import re
import shutil

from pathlib import Path

from runners.core import MissingProgram, RunnerCaps, RunnerConfig, ZephyrBinaryRunner

STLINK_GDB_SERVER_DEFAULT_PORT = 61234


class STLinkGDBServerRunner(ZephyrBinaryRunner):
    @classmethod
    def _get_stm32cubeclt_paths(cls) -> tuple[Path, Path]:
        """
        Returns a tuple of two elements of class pathlib.Path:
            [0]: path to the ST-LINK_gdbserver executable
            [1]: path to the "STM32CubeProgrammer/bin" folder
        """

        def find_highest_clt_version(tools_folder: Path) -> Path | None:
            if not tools_folder.is_dir():
                return None

            # List all CubeCLT installations present in tools folder
            CUBECLT_FLDR_RE = re.compile(r"stm32cubeclt_([1-9]).(\d+).(\d+)", re.IGNORECASE)
            installations: list[tuple[int, Path]] = []
            for f in tools_folder.iterdir():
                m = CUBECLT_FLDR_RE.match(f.name)
                if m is not None:
                    # Compute a number that can be easily compared
                    # from the STM32CubeCLT version number
                    major, minor, revis = int(m[1]), int(m[2]), int(m[3])
                    ver_num = major * 1000000 + minor * 1000 + revis
                    installations.append((ver_num, f))

            if len(installations) == 0:
                return None

            # Sort candidates and return the path to the most recent version
            most_recent_install = sorted(installations, key=lambda e: e[0], reverse=True)[0]
            return most_recent_install[1]

        cur_platform = platform.system()

        # Attempt to find via shutil.which()
        if cur_platform in ["Linux", "Windows"]:
            gdbserv = shutil.which("ST-LINK_gdbserver")
            cubeprg = shutil.which("STM32_Programmer_CLI")
            if gdbserv and cubeprg:
                # Return the parent of cubeprg as [1] should be the path
                # to the folder containing STM32_Programmer_CLI, not the
                # path to the executable itself
                return (Path(gdbserv), Path(cubeprg).parent)

        # Search in OS-specific paths
        search_path: str
        tool_suffix = ""
        if cur_platform == "Linux":
            search_path = "/opt/st/"
        elif cur_platform == "Windows":
            search_path = "C:\\ST\\"
            tool_suffix = ".exe"
        elif cur_platform == "Darwin":
            search_path = "/opt/ST/"
        else:
            raise RuntimeError("Unsupported OS")

        clt = find_highest_clt_version(Path(search_path))
        if clt is None:
            raise MissingProgram("ST-LINK_gdbserver (from STM32CubeCLT)")

        gdbserver_path = clt / "STLink-gdb-server" / "bin" / f"ST-LINK_gdbserver{tool_suffix}"
        cubeprg_bin_path = clt / "STM32CubeProgrammer" / "bin"

        return (gdbserver_path, cubeprg_bin_path)

    @classmethod
    def name(cls) -> str:
        return "stlink_gdbserver"

    @classmethod
    def capabilities(cls) -> RunnerCaps:
        return RunnerCaps(commands={"attach", "debug", "debugserver"}, dev_id=True, extload=True)

    @classmethod
    def extload_help(cls) -> str:
        return "External Loader for ST-Link GDB server"

    @classmethod
    def do_add_parser(cls, parser: argparse.ArgumentParser):
        # Expose a subset of the ST-LINK GDB server arguments
        parser.add_argument(
            "--swd",
            default=True,
            action=argparse.BooleanOptionalAction,
            help="Enable SWD debug mode (default: %(default)s)\nUse --no-swd to disable.",
        )
        parser.add_argument("--apid", type=int, default=0, help="Target DAP ID")
        parser.add_argument(
            "--port-number",
            type=int,
            default=STLINK_GDB_SERVER_DEFAULT_PORT,
            help="Port number for GDB client",
        )
        parser.add_argument(
            "--external-init",
            action='store_true',
            help="Run Init() from external loader after reset",
        )

        parser.add_argument('--two-boot-stage', action='store_true',
                          help='Debug application running from flash with special boot-chain (MCUboot mode first)')
        parser.add_argument('--mcuboot-offset', type=str,
                          help='MCUboot image offset (e.g., 0x08040000)')

    @classmethod
    def do_create(cls, cfg: RunnerConfig, args: argparse.Namespace) -> "STLinkGDBServerRunner":
        return STLinkGDBServerRunner(
            cfg,
            args.swd,
            args.apid,
            args.dev_id,
            args.port_number,
            args.extload,
            args.external_init,
            args.two_boot_stage,
            args.mcuboot_offset,
        )

    def __init__(
        self,
        cfg: RunnerConfig,
        swd: bool,
        ap_id: int | None,
        stlink_serial: str | None,
        gdb_port: int,
        external_loader: str | None,
        external_init: bool,
        two_boot_stage: bool,
        mcuboot_offset: str|None,
    ):
        super().__init__(cfg)
        self.ensure_output('elf')

        self._swd = swd
        self._gdb_port = gdb_port
        self._stlink_serial = stlink_serial
        self._ap_id = ap_id
        self._external_loader = external_loader
        self._do_external_init = external_init
        self._two_boot_stage = two_boot_stage
        self._mcuboot_offset = mcuboot_offset or '0x08040000'

    def do_run(self, command: str, **kwargs):
        if command in ["attach", "debug", "debugserver"]:
            self.do_attach_debug_debugserver(command)
        else:
            raise ValueError(f"{command} not supported")
    
    def _get_start_address(self) -> int:
        """Get the start address from ELF file entry point."""
        import subprocess

        try:
            result = subprocess.run(
                ['arm-none-eabi-readelf', '-h', self.cfg.elf_file],
                capture_output=True,
                text=True,
                check=True
            )

            # Parse "Entry point address: 0x34180749"
            for line in result.stdout.split('\n'):
                if 'Entry point address' in line:
                    addr_str = line.split(':')[1].strip()
                    return int(addr_str, 16)

        except Exception as e:
            self.logger.warning(f"Could not read entry point: {e}")

        return 0

    def _is_fsbl_mode(self, start_address: int) -> bool:
        """Check if application is in FSBL memory region (0x34180000-0x3418FFFF)."""
        return (start_address & 0xFFFF0000) == 0x34180000
    def _is_ram_mode(self, start_address: int) -> bool:
        """Check if application is in RAM region (not FSBL)."""
        # RAM region: 0x34000000 - 0x3417FFFF
        return 0x34000000 <= start_address < 0x34180000


    def _get_stm32_soc(self) -> str:
        """
        Get STM32 SoC family from build config.

        Returns:
            'STM32N6', 'STM32H7', 'STM32F4', etc. or 'unknown'
        """
        from pathlib import Path

        config_file = Path(self.cfg.build_dir) / 'zephyr' / '.config'

        if not config_file.exists():
            return 'unknown'

        try:
            with open(config_file, 'r') as f:
                for line in f:
                    # Look for CONFIG_SOC_SERIES_STM32XXX=y
                    if line.startswith('CONFIG_SOC_SERIES_STM32') and '=y' in line:
                        # Extract: CONFIG_SOC_SERIES_STM32N6X=y → STM32N6X
                        soc_series = line.split('=')[0].replace('CONFIG_SOC_SERIES_', '').strip()
                        # Return just the family: STM32N6X → STM32N6
                        return soc_series[:7]  # 'STM32N6' from 'STM32N6X'

        except Exception as e:
            pass

        return 'unknown'

    def _find_mcuboot_elf(self) -> str:
        """Find MCUboot ELF file in sysbuild output."""

        # Sysbuild puts MCUboot at: build/mcuboot/zephyr/zephyr.elf
        mcuboot_elf = Path(self.cfg.build_dir) / 'mcuboot' / 'zephyr' / 'zephyr.elf'

        if mcuboot_elf.exists():
            return mcuboot_elf.as_posix()

        # Alternative location for older Zephyr
        mcuboot_elf_alt = Path(self.cfg.build_dir).parent / 'mcuboot' / 'zephyr' / 'zephyr.elf'
        if mcuboot_elf_alt.exists():
            return mcuboot_elf_alt.as_posix()

        raise RuntimeError(
            "MCUboot ELF not found. For --flash mode with sysbuild, "
            "MCUboot must be built (use --sysbuild flag)"
        )

    def _create_mcuboot_debug_script(self, app_elf_path: str) -> str:
        """Create temporary GDB script for MCUboot two-phase debugging."""
        import tempfile
    
        # Create temporary script
        script_content = f"""
            # ============================================================
            # PHASE 1: RUN MCUBOOT AND INITIALIZE EXTERNAL FLASH
            # ============================================================
            target remote :{self._gdb_port}
            #monitor reset halt
            
            # Load MCUboot into device
            load
            
            # Break at boot_go (before jumping to app)
            hbreak boot_go
            continue
            
            # Step through external flash initialization
            next
            next
            
            # ============================================================
            # PHASE 2: EXTERNAL FLASH READY - LOAD APP SYMBOLS
            # ============================================================
            
            # Add application symbols at correct offset
            add-symbol-file {app_elf_path} {self._mcuboot_offset}
            
            # Set breakpoint in app
            break main
            
            # Continue into application
            continue
            """
    
        # Write to temporary file
        fd, script_path = tempfile.mkstemp(suffix='.gdb', text=True)
        with os.fdopen(fd, 'w') as f:
            f.write(script_content)
        
        return script_path



    def do_attach_debug_debugserver(self, command: str):
        # self.ensure_output('elf') is called in constructor
        # and validated that self.cfg.elf_file is non-null.
        # This assertion is required for the test framework,
        # which doesn't have this insight - it should never
        # trigger in real-world scenarios.
        assert self.cfg.elf_file is not None
        elf_path = Path(self.cfg.elf_file).as_posix()
            
        # Detect SoC and execution mode
        soc = self._get_stm32_soc()
        start_address = self._get_start_address()
        is_fsbl = self._is_fsbl_mode(start_address)
        is_ram = self._is_ram_mode(start_address)
        
        self.logger.info(f"SoC: {soc}, Entry: 0x{start_address:08x}, FSBL: {is_fsbl}")

        gdb_args = ["-ex", f"target remote :{self._gdb_port}", elf_path]

        (gdbserver_path, cubeprg_path) = STLinkGDBServerRunner._get_stm32cubeclt_paths()
        gdbserver_cmd = [gdbserver_path.as_posix()]
        gdbserver_cmd += ["--stm32cubeprogrammer-path", str(cubeprg_path.absolute())]
        gdbserver_cmd += ["--port-number", str(self._gdb_port)]
        gdbserver_cmd += ["--apid", str(self._ap_id)]
        gdbserver_cmd += ["--halt"]

        if self._swd:
            gdbserver_cmd.append("--swd")

        if command == "attach":
            gdbserver_cmd += ["--attach"]
        
        # STM32N6 two boot layer stage commands
        elif not is_fsbl and not is_ram and (soc=='STM32N6'):
        #elif self._two_boot_stage and command == "debug":
            # Flash mode: Debug MCUboot app already in flash
            gdbserver_cmd += ["--attach"]  # Don't reset!
            
            # Create GDB script for two-phase debug
            #gdb_script = self._create_mcuboot_debug_script(elf_path)
            
            # Find MCUboot ELF
            mcuboot_elf = self._find_mcuboot_elf()
                
            # Start GDB with MCUboot symbols, script handles the rest
            gdb_args =[
                "-ex", f"target remote :{self._gdb_port}",
                "-ex", "load",
                "-ex", "hbreak boot_go",
                "-ex", "continue",
                "-ex", "set confirm off",
                "-ex", f"add-symbol-file {elf_path}",
                "-ex", "set confirm on",
                "-ex", "break main",
                "-ex", "continue",
                mcuboot_elf,
            ]
        
        else:  
            gdbserver_cmd += ["--initialize-reset"]
            gdb_args += ["-ex", f"load {elf_path}"]

        if self._stlink_serial:
            gdbserver_cmd += ["--serial-number", self._stlink_serial]

        if self._external_loader:
            extldr_path = cubeprg_path / "ExternalLoader" / self._external_loader
            if not extldr_path.exists():
                raise RuntimeError(f"External loader {self._external_loader} does not exist")

            if self._do_external_init:
                gdbserver_cmd += ["--external-init"]
            gdbserver_cmd += ["--extload", str(extldr_path)]

        self.require(gdbserver_cmd[0])

        if command == "debugserver":
            self.check_call(gdbserver_cmd)
        elif self.cfg.gdb is None:  # attach/debug
            raise RuntimeError("GDB is required for attach/debug")
        else:  # attach/debug
            gdb_cmd = [self.cfg.gdb] + gdb_args
            self.require(gdb_cmd[0])
            self.run_server_and_client(gdbserver_cmd, gdb_cmd)
