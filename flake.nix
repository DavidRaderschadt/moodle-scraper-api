{
  description = "Moodle DHBW API Scraper";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
        pythonEnv = python.withPackages (ps: with ps; [
          fastapi
          uvicorn
          requests
          beautifulsoup4
          lxml
          apscheduler
          pytz
        ]);
      in {
        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv pkgs.uv pkgs.docker-compose ];
          shellHook = ''
            export PYTHONPATH="$(pwd)"
            export DOWNLOAD_DIR="$(pwd)/data/files"
            export STATE_FILE="$(pwd)/data/.state.json"
            mkdir -p data/files
            if [ ! -f .env ]; then
              cp .env.example .env
              echo "created .env — fill in MOODLE_USERNAME and MOODLE_PASSWORD"
            fi
          '';
        };

        apps.default = {
          type = "app";
          program = "${pkgs.writeShellScript "moodle-api" ''
            export PYTHONPATH="$(dirname "$0")/.."
            source .env 2>/dev/null || true
            exec ${pythonEnv}/bin/uvicorn src.main:app --host 0.0.0.0 --port 8000 "$@"
          ''}";
        };
      }
    );
}
