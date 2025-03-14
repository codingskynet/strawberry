import abc
import asyncio
import contextlib
import json
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)
from typing_extensions import Literal

from graphql import GraphQLError

from strawberry import UNSET
from strawberry.exceptions import MissingQueryError
from strawberry.file_uploads.utils import replace_placeholders_with_files
from strawberry.http import (
    GraphQLHTTPResponse,
    GraphQLRequestData,
    process_result,
)
from strawberry.http.ides import GraphQL_IDE
from strawberry.schema.base import BaseSchema
from strawberry.schema.exceptions import InvalidOperationTypeError
from strawberry.types import ExecutionResult, SubscriptionExecutionResult
from strawberry.types.graphql import OperationType

from .base import BaseView
from .exceptions import HTTPException
from .parse_content_type import parse_content_type
from .types import FormData, HTTPMethod, QueryParams
from .typevars import Context, Request, Response, RootValue, SubResponse


class AsyncHTTPRequestAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def query_params(self) -> QueryParams: ...

    @property
    @abc.abstractmethod
    def method(self) -> HTTPMethod: ...

    @property
    @abc.abstractmethod
    def headers(self) -> Mapping[str, str]: ...

    @property
    @abc.abstractmethod
    def content_type(self) -> Optional[str]: ...

    @abc.abstractmethod
    async def get_body(self) -> Union[str, bytes]: ...

    @abc.abstractmethod
    async def get_form_data(self) -> FormData: ...


class AsyncBaseHTTPView(
    abc.ABC,
    BaseView[Request],
    Generic[Request, Response, SubResponse, Context, RootValue],
):
    schema: BaseSchema
    graphql_ide: Optional[GraphQL_IDE]
    request_adapter_class: Callable[[Request], AsyncHTTPRequestAdapter]

    @property
    @abc.abstractmethod
    def allow_queries_via_get(self) -> bool: ...

    @abc.abstractmethod
    async def get_sub_response(self, request: Request) -> SubResponse: ...

    @abc.abstractmethod
    async def get_context(self, request: Request, response: SubResponse) -> Context: ...

    @abc.abstractmethod
    async def get_root_value(self, request: Request) -> Optional[RootValue]: ...

    @abc.abstractmethod
    def create_response(
        self, response_data: GraphQLHTTPResponse, sub_response: SubResponse
    ) -> Response: ...

    @abc.abstractmethod
    async def render_graphql_ide(self, request: Request) -> Response: ...

    async def create_streaming_response(
        self,
        request: Request,
        stream: Callable[[], AsyncGenerator[str, None]],
        sub_response: SubResponse,
        headers: Dict[str, str],
    ) -> Response:
        raise ValueError("Multipart responses are not supported")

    async def execute_operation(
        self, request: Request, context: Context, root_value: Optional[RootValue]
    ) -> Union[ExecutionResult, SubscriptionExecutionResult]:
        request_adapter = self.request_adapter_class(request)

        try:
            request_data = await self.parse_http_body(request_adapter)
        except json.decoder.JSONDecodeError as e:
            raise HTTPException(400, "Unable to parse request body as JSON") from e
            # DO this only when doing files
        except KeyError as e:
            raise HTTPException(400, "File(s) missing in form data") from e

        allowed_operation_types = OperationType.from_http(request_adapter.method)

        if not self.allow_queries_via_get and request_adapter.method == "GET":
            allowed_operation_types = allowed_operation_types - {OperationType.QUERY}

        assert self.schema

        if request_data.protocol == "multipart-subscription":
            return await self.schema.subscribe(
                request_data.query,  # type: ignore
                variable_values=request_data.variables,
                context_value=context,
                root_value=root_value,
                operation_name=request_data.operation_name,
            )

        return await self.schema.execute(
            request_data.query,
            root_value=root_value,
            variable_values=request_data.variables,
            context_value=context,
            operation_name=request_data.operation_name,
            allowed_operation_types=allowed_operation_types,
        )

    async def parse_multipart(self, request: AsyncHTTPRequestAdapter) -> Dict[str, str]:
        try:
            form_data = await request.get_form_data()
        except ValueError as e:
            raise HTTPException(400, "Unable to parse the multipart body") from e

        operations = form_data["form"].get("operations", "{}")
        files_map = form_data["form"].get("map", "{}")

        if isinstance(operations, (bytes, str)):
            operations = self.parse_json(operations)

        if isinstance(files_map, (bytes, str)):
            files_map = self.parse_json(files_map)

        try:
            return replace_placeholders_with_files(
                operations, files_map, form_data["files"]
            )
        except KeyError as e:
            raise HTTPException(400, "File(s) missing in form data") from e

    def _handle_errors(
        self, errors: List[GraphQLError], response_data: GraphQLHTTPResponse
    ) -> None:
        """Hook to allow custom handling of errors, used by the Sentry Integration."""

    async def run(
        self,
        request: Request,
        context: Optional[Context] = UNSET,
        root_value: Optional[RootValue] = UNSET,
    ) -> Response:
        request_adapter = self.request_adapter_class(request)

        if not self.is_request_allowed(request_adapter):
            raise HTTPException(405, "GraphQL only supports GET and POST requests.")

        if self.should_render_graphql_ide(request_adapter):
            if self.graphql_ide:
                return await self.render_graphql_ide(request)
            else:
                raise HTTPException(404, "Not Found")

        sub_response = await self.get_sub_response(request)
        context = (
            await self.get_context(request, response=sub_response)
            if context is UNSET
            else context
        )
        root_value = (
            await self.get_root_value(request) if root_value is UNSET else root_value
        )

        assert context

        try:
            result = await self.execute_operation(
                request=request, context=context, root_value=root_value
            )
        except InvalidOperationTypeError as e:
            raise HTTPException(
                400, e.as_http_error_reason(request_adapter.method)
            ) from e
        except MissingQueryError as e:
            raise HTTPException(400, "No GraphQL query found in the request") from e

        if isinstance(result, SubscriptionExecutionResult):
            stream = self._get_stream(request, result)

            return await self.create_streaming_response(
                request,
                stream,
                sub_response,
                headers={
                    "Transfer-Encoding": "chunked",
                    "Content-Type": "multipart/mixed;boundary=graphql;subscriptionSpec=1.0,application/json",
                },
            )

        response_data = await self.process_result(request=request, result=result)

        if result.errors:
            self._handle_errors(result.errors, response_data)

        return self.create_response(
            response_data=response_data, sub_response=sub_response
        )

    def encode_multipart_data(self, data: Any, separator: str) -> str:
        return "".join(
            [
                f"\r\n--{separator}\r\n",
                "Content-Type: application/json\r\n\r\n",
                self.encode_json(data),
                "\n",
            ]
        )

    def _stream_with_heartbeat(
        self, stream: Callable[[], AsyncGenerator[str, None]]
    ) -> Callable[[], AsyncGenerator[str, None]]:
        """Adds a heartbeat to the stream, to prevent the connection from closing when there are no messages being sent."""
        queue: asyncio.Queue[Tuple[bool, Any]] = asyncio.Queue(1)

        cancelling = False

        async def drain() -> None:
            try:
                async for item in stream():
                    await queue.put((False, item))
            except Exception as e:
                if not cancelling:
                    await queue.put((True, e))
                else:
                    raise

        async def heartbeat() -> None:
            while True:
                await queue.put((False, self.encode_multipart_data({}, "graphql")))

                await asyncio.sleep(5)

        async def merged() -> AsyncGenerator[str, None]:
            heartbeat_task = asyncio.create_task(heartbeat())
            task = asyncio.create_task(drain())

            async def cancel_tasks() -> None:
                nonlocal cancelling
                cancelling = True
                task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await task

                heartbeat_task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

            try:
                while not task.done():
                    raised, data = await queue.get()

                    if raised:
                        await cancel_tasks()
                        raise data

                    yield data
            finally:
                await cancel_tasks()

        return merged

    def _get_stream(
        self,
        request: Request,
        result: SubscriptionExecutionResult,
        separator: str = "graphql",
    ) -> Callable[[], AsyncGenerator[str, None]]:
        async def stream() -> AsyncGenerator[str, None]:
            async for value in result:
                response = await self.process_result(request, value)
                yield self.encode_multipart_data({"payload": response}, separator)

            yield f"\r\n--{separator}--\r\n"

        return self._stream_with_heartbeat(stream)

    async def parse_multipart_subscriptions(
        self, request: AsyncHTTPRequestAdapter
    ) -> Dict[str, str]:
        if request.method == "GET":
            return self.parse_query_params(request.query_params)

        return self.parse_json(await request.get_body())

    async def parse_http_body(
        self, request: AsyncHTTPRequestAdapter
    ) -> GraphQLRequestData:
        content_type, params = parse_content_type(request.content_type or "")

        protocol: Literal["http", "multipart-subscription"] = "http"

        if request.method == "GET":
            data = self.parse_query_params(request.query_params)
            if self._is_multipart_subscriptions(content_type, params):
                protocol = "multipart-subscription"
        elif "application/json" in content_type:
            data = self.parse_json(await request.get_body())
        elif content_type == "multipart/form-data":
            data = await self.parse_multipart(request)
        elif self._is_multipart_subscriptions(content_type, params):
            data = await self.parse_multipart_subscriptions(request)
            protocol = "multipart-subscription"
        else:
            raise HTTPException(400, "Unsupported content type")

        return GraphQLRequestData(
            query=data.get("query"),
            variables=data.get("variables"),
            operation_name=data.get("operationName"),
            protocol=protocol,
        )

    async def process_result(
        self, request: Request, result: ExecutionResult
    ) -> GraphQLHTTPResponse:
        return process_result(result)


__all__ = ["AsyncBaseHTTPView"]
