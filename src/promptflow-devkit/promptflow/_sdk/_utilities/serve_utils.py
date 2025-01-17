import abc
import contextlib
import json
import logging
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from promptflow._constants import PROMPT_FLOW_DIR_NAME, FlowLanguage
from promptflow._proxy._csharp_inspector_proxy import EXECUTOR_SERVICE_DLL
from promptflow._utils.flow_utils import resolve_flow_path
from promptflow.exceptions import UserErrorException
from promptflow.tracing import start_trace

from .general_utils import resolve_flow_language

logger = logging.getLogger(__name__)


class ServeAppHelper(abc.ABC):
    """The abstract class for serve app helper.

    help to start and terminate the serve app.
    """

    def __init__(
        self, *, flow_file_name: str, flow_dir: Path, init: Dict[str, Any], port: int, host: str = "localhost", **kwargs
    ):
        self._flow_file_name = flow_file_name
        self._flow_dir = flow_dir
        self._init = init or {}
        self._port = port
        self._host = host

    @abc.abstractmethod
    def start_in_main(self, skip_open_browser: bool = False):
        """Start the serve app in main thread."""
        pass

    @abc.abstractmethod
    def start(self):
        """Start the serve app in a subprocess."""
        pass

    @abc.abstractmethod
    def terminate(self):
        """Terminate the serve app in subprocess."""
        pass


class PythonServeAppHelper(ServeAppHelper):
    def __init__(
        self, *, flow_file_name: str, flow_dir: Path, init: Dict[str, Any], port: int, host: str = "localhost", **kwargs
    ):
        self._static_folder: Optional[str] = kwargs.get("static_folder", None)
        self._config = kwargs.get("config", {}) or {}
        self._environment_variables = kwargs.get("environment_variables", {}) or {}
        self._engine = kwargs.get("engine", "flask")

        super().__init__(flow_file_name=flow_file_name, flow_dir=flow_dir, init=init, port=port, host=host, **kwargs)

        self._process: Optional[multiprocessing.Process] = None

    def _run(self, skip_open_browser: bool = False, enable_trace: bool = False):
        if enable_trace:
            # trace must be started within the same process as the app
            start_trace()

        error_msg = None
        try:
            flow_dir, flow_file_name = resolve_flow_path(self._flow_dir, allow_prompty_dir=True)
            if flow_file_name != self._flow_file_name:
                # this may happen when a prompty is specified while there is already a default flow in the flow dir
                error_msg = (
                    f"Default definition {flow_file_name} is found and will be picked in flow directory "
                    f"while {self._flow_file_name} is specified. "
                    f"Please remove {flow_file_name} from flow directory as a workaround."
                )
        except (UserErrorException,) as e:
            error_msg = e.message

        if error_msg is not None:
            raise UserErrorException(
                message_format="Service have some limitations on flow directory for now:\n{msg}",
                msg=error_msg,
                privacy_info=[self._flow_dir.absolute().as_posix()],
            )

        serve_python_flow(
            flow_file_name=self._flow_file_name,
            flow_dir=self._flow_dir,
            port=self._port,
            host=self._host,
            static_folder=self._static_folder,
            config=self._config,
            environment_variables=self._environment_variables,
            init=self._init,
            skip_open_browser=skip_open_browser,
            engine=self._engine,
        )

    def start_in_main(self, skip_open_browser: bool = False):
        self._run(skip_open_browser=skip_open_browser)

    def start(self):
        self._process = multiprocessing.Process(
            target=self._run,
            # no need to open browser if the serve app is started in a subprocess
            kwargs={"skip_open_browser": True, "enable_trace": True},
        )
        self._process.start()

    def terminate(self):
        if self._process:
            self._process.terminate()
            self._process.join()


class CSharpServeAppHelper(ServeAppHelper):
    def __init__(
        self, *, flow_file_name: str, flow_dir: Path, init: Dict[str, Any], port: int, host: str = "localhost", **kwargs
    ):
        super().__init__(flow_file_name=flow_file_name, flow_dir=flow_dir, init=init, port=port, host=host, **kwargs)

        self._process: Optional[subprocess.Popen] = None

    @contextlib.contextmanager
    def _construct_start_up_command(self) -> Generator[str, None, None]:
        cmd = [
            "dotnet",
            EXECUTOR_SERVICE_DLL,
            "--port",
            str(self._port),
            "--yaml_path",
            self._flow_file_name,
            "--assembly_folder",
            ".",
            "--connection_provider_url",
            "",
            "--log_path",
            "",
            "--serving",
        ]
        if self._init:
            init_json_path = self._flow_dir / PROMPT_FLOW_DIR_NAME / f"init-{uuid.uuid4()}.json"
            init_json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(init_json_path, "w") as f:
                json.dump(self._init, f)
            cmd.extend(["--init", init_json_path.as_posix()])
            try:
                yield cmd
            finally:
                os.remove(init_json_path)
        else:
            yield cmd

    def start_in_main(self, skip_open_browser: bool = False):
        # TODO: open browser after default chat UI is available for CSharp
        try:
            with self._construct_start_up_command() as command:
                subprocess.run(command, cwd=self._flow_dir, stdout=sys.stdout, stderr=sys.stderr)
        except KeyboardInterrupt:
            pass

    def start(self):
        with self._construct_start_up_command() as command:
            self._process = subprocess.Popen(command, cwd=self._flow_dir, stdout=sys.stdout, stderr=sys.stderr)

    def terminate(self):
        if self._process:
            self._process.terminate()
            self._process.wait()


def find_available_port() -> str:
    """Find an available port on localhost"""
    # TODO: replace find_available_port in CSharpExecutorProxy with this one
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        _, port = s.getsockname()
        return str(port)


def _resolve_python_flow_additional_includes(flow_file_name: str, flow_dir: Path) -> Path:
    # Resolve flow additional includes
    from promptflow._sdk.operations import FlowOperations

    flow_path = Path(flow_dir) / flow_file_name
    with FlowOperations._resolve_additional_includes(flow_path) as resolved_flow_path:
        if resolved_flow_path == flow_path:
            return flow_dir
        # Copy resolved flow to temp folder if additional includes exists
        # Note: DO NOT use resolved flow path directly, as when inner logic raise exception,
        # temp dir will fail due to file occupied by other process.
        temp_flow_path = Path(tempfile.TemporaryDirectory().name)
        shutil.copytree(src=resolved_flow_path.parent, dst=temp_flow_path, dirs_exist_ok=True)

    return temp_flow_path


def start_flow_service(
    *,
    source: Path,
    static_folder: str = None,
    host: str = "localhost",
    port: int = 8080,
    config: dict = None,
    environment_variables: Dict[str, str] = None,
    init: Dict[str, Any] = None,
    skip_open_browser: bool = True,
    engine: str = "flask",
):
    logger.info(
        "Start promptflow server with port %s",
        port,
    )

    flow_dir, flow_file_name = resolve_flow_path(source, allow_prompty_dir=True)
    # prompty dir works for resolve_flow_path, but not for resolve_flow_language,
    # so infer language after resolve_flow_path
    language = resolve_flow_language(flow_path=flow_dir / flow_file_name)

    if language == FlowLanguage.Python:
        if not os.path.isdir(source):
            raise UserErrorException(
                message_format="Support directory `source` for Python flow only for now, but got {source}.",
                source=source,
            )
        if engine not in ["flask", "fastapi"]:
            raise UserErrorException(
                message_format="Unsupported engine {engine} for Python flow, only support 'flask' and 'fastapi'.",
                engine=engine,
            )

        helper = PythonServeAppHelper(
            flow_file_name=flow_file_name,
            flow_dir=flow_dir,
            init=init,
            port=port,
            host=host,
            static_folder=Path(static_folder).absolute().as_posix() if static_folder else None,
            config=config or {},
            environment_variables=environment_variables or {},
            engine=engine,
        )
    else:
        helper = CSharpServeAppHelper(
            flow_file_name=flow_file_name,
            flow_dir=flow_dir,
            init=init or {},
            port=port,
            host=host,
        )
    helper.start_in_main(skip_open_browser=skip_open_browser)


def serve_python_flow(
    *,
    flow_file_name,
    flow_dir,
    port,
    host,
    static_folder,
    config,
    environment_variables,
    init,
    skip_open_browser: bool,
    engine,
):
    # we should consider moving below logic to PythonServeAppHelper._run but keep it here for now as it's not related to
    # the helper itself
    from promptflow._sdk._configuration import Configuration
    from promptflow.core._serving.app import create_app

    # if no additional includes, flow_dir keeps the same; if additional includes, flow_dir is a temp dir
    flow_dir = _resolve_python_flow_additional_includes(flow_file_name, flow_dir)

    pf_config = Configuration(overrides=config)
    logger.info(f"Promptflow config: {pf_config}")
    connection_provider = pf_config.get_connection_provider()
    os.environ["PROMPTFLOW_PROJECT_PATH"] = flow_dir.absolute().as_posix()
    logger.info(f"Change working directory to model dir {flow_dir}")
    os.chdir(flow_dir)
    app = create_app(
        static_folder=Path(static_folder).absolute().as_posix() if static_folder else None,
        environment_variables=environment_variables,
        connection_provider=connection_provider,
        init=init,
        engine=engine,
    )
    if not skip_open_browser:
        target = f"http://{host}:{port}"
        logger.info(f"Opening browser {target}...")
        webbrowser.open(target)
    # Debug is not supported for now as debug will rerun command, and we changed working directory.
    if engine == "flask":
        app.run(port=port, host=host)
    else:
        try:
            import uvicorn

            uvicorn.run(app, host=host, port=port, access_log=False, log_config=None)
        except ImportError:
            raise UserErrorException(
                message_format="FastAPI engine requires uvicorn, please install uvicorn by `pip install uvicorn`."
            )
