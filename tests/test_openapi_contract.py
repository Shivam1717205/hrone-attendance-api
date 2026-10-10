import unittest
from pathlib import Path

import yaml

import app.main as main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_reference(document, value):
    target = document
    for part in value["$ref"].lstrip("#/").split("/"):
        target = target[part]
    return target


def resolve_schema(document, schema):
    if "$ref" in schema:
        return resolve_schema(document, resolve_reference(document, schema))
    if "allOf" in schema:
        merged = {}
        for part in schema["allOf"]:
            merged.update(resolve_schema(document, part))
        merged.update({key: value for key, value in schema.items() if key != "allOf"})
        return merged
    return schema


def schema_type(document, schema):
    schema = resolve_schema(document, schema)
    if "type" in schema:
        return schema["type"]
    types = {
        schema_type(document, item)
        for item in schema.get("anyOf", [])
        if schema_type(document, item) != "null"
    }
    return next(iter(types)) if len(types) == 1 else None


def non_null_schema(document, schema):
    schema = resolve_schema(document, schema)
    variants = schema.get("anyOf", [])
    if variants:
        for variant in variants:
            if schema_type(document, variant) != "null":
                return resolve_schema(document, variant)
    return schema


def assert_schema_matches(test_case, contract, runtime, wanted, got, location):
    wanted = resolve_schema(contract, wanted)
    got_original = resolve_schema(runtime, got)
    is_nullable = any(
        schema_type(runtime, option) == "null"
        for option in got_original.get("anyOf", [])
    )
    got = non_null_schema(runtime, got_original)
    if "type" in wanted:
        test_case.assertEqual(
            schema_type(runtime, got),
            schema_type(contract, wanted),
            f"{location} type",
        )
    if wanted.get("nullable"):
        test_case.assertTrue(is_nullable, f"{location} must be nullable")
    for key in ("format", "pattern", "minimum", "maximum", "minLength", "maxLength"):
        if key in wanted:
            test_case.assertEqual(got.get(key), wanted[key], f"{location} {key}")
    expected_enum = wanted.get("enum")
    actual_enum = got.get("enum")
    if expected_enum is not None:
        if actual_enum is None and "const" in got:
            actual_enum = [got["const"]]
        test_case.assertEqual(actual_enum, expected_enum, f"{location} enum")
    if "required" in wanted:
        test_case.assertEqual(
            set(got.get("required", [])),
            set(wanted["required"]),
            f"{location} required fields",
        )
    if "properties" in wanted:
        test_case.assertEqual(
            set(got.get("properties", {})),
            set(wanted["properties"]),
            f"{location} fields",
        )
        for name, child in wanted["properties"].items():
            assert_schema_matches(
                test_case,
                contract,
                runtime,
                child,
                got["properties"][name],
                f"{location}.{name}",
            )
    if "items" in wanted:
        test_case.assertIn("items", got, f"{location} array items")
        assert_schema_matches(
            test_case,
            contract,
            runtime,
            wanted["items"],
            got["items"],
            f"{location}[]",
        )
    if wanted.get("additionalProperties") is True:
        test_case.assertIs(got.get("additionalProperties"), True, f"{location} additionalProperties")


class OpenAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load((PROJECT_ROOT / "openapi.yaml").read_text())
        cls.runtime = main.app.openapi()

    def test_all_contract_routes_methods_and_documented_statuses_are_present(self):
        contract_paths = self.contract["paths"]
        runtime_paths = self.runtime["paths"]
        framework_paths = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
        self.assertEqual(set(runtime_paths) - framework_paths, set(contract_paths))

        for path, operations in contract_paths.items():
            for method, expected in operations.items():
                with self.subTest(path=path, method=method):
                    actual = runtime_paths[path][method]
                    self.assertTrue(
                        set(expected["responses"]).issubset(actual["responses"]),
                        f"missing documented responses: {set(expected['responses']) - set(actual['responses'])}",
                    )

    def test_parameters_match_names_locations_requiredness_and_constraints(self):
        for path, operations in self.contract["paths"].items():
            for method, expected in operations.items():
                actual = self.runtime["paths"][path][method]
                expected_params = [
                    resolve_reference(self.contract, item) if "$ref" in item else item
                    for item in expected.get("parameters", [])
                ]
                actual_params = actual.get("parameters", [])
                self.assertEqual(
                    {(item["name"], item["in"], item.get("required", False)) for item in actual_params},
                    {(item["name"], item["in"], item.get("required", False)) for item in expected_params},
                    f"{method.upper()} {path} parameters",
                )
                actual_by_name = {item["name"]: item for item in actual_params}
                for expected_param in expected_params:
                    wanted = resolve_schema(self.contract, expected_param["schema"])
                    got = non_null_schema(
                        self.runtime,
                        actual_by_name[expected_param["name"]]["schema"],
                    )
                    if "type" in wanted:
                        self.assertEqual(
                            schema_type(self.runtime, got),
                            wanted["type"],
                            f"{path} parameter {expected_param['name']} type",
                        )
                    for key in ("format", "enum", "pattern", "minimum", "maximum", "default"):
                        if key in wanted:
                            self.assertEqual(
                                got.get(key),
                                wanted[key],
                                f"{path} parameter {expected_param['name']} {key}",
                            )

    def test_request_body_fields_and_constraints_match_contract(self):
        for path, operations in self.contract["paths"].items():
            for method, expected in operations.items():
                if "requestBody" not in expected:
                    continue
                actual = self.runtime["paths"][path][method]["requestBody"]
                self.assertEqual(actual.get("required", False), expected["requestBody"].get("required", False))
                wanted = resolve_schema(
                    self.contract,
                    expected["requestBody"]["content"]["application/json"]["schema"],
                )
                got = resolve_schema(
                    self.runtime,
                    actual["content"]["application/json"]["schema"],
                )
                self.assertEqual(set(got.get("required", [])), set(wanted.get("required", [])))
                self.assertEqual(set(got["properties"]), set(wanted["properties"]))
                for name, wanted_property in wanted["properties"].items():
                    wanted_property = resolve_schema(self.contract, wanted_property)
                    got_property = resolve_schema(self.runtime, got["properties"][name])
                    self.assertEqual(
                        schema_type(self.contract, wanted_property),
                        schema_type(self.runtime, got_property),
                        f"{path} request {name} type",
                    )
                    for key in ("format", "enum", "pattern", "minimum", "maximum", "minLength", "maxLength", "default"):
                        if key in wanted_property:
                            self.assertEqual(
                                got_property.get(key),
                                wanted_property[key],
                                f"{path} request {name} {key}",
                            )

    def test_success_and_error_response_shapes_match_contract(self):
        for path, operations in self.contract["paths"].items():
            for method, expected in operations.items():
                actual = self.runtime["paths"][path][method]
                for status, response in expected["responses"].items():
                    if "$ref" in response:
                        response = resolve_reference(self.contract, response)
                    expected_content = response.get("content", {}).get("application/json")
                    if not expected_content:
                        continue
                    with self.subTest(path=path, method=method, status=status):
                        actual_content = actual["responses"][status]["content"]["application/json"]
                        wanted = resolve_schema(self.contract, expected_content["schema"])
                        assert_schema_matches(
                            self,
                            self.contract,
                            self.runtime,
                            wanted,
                            actual_content["schema"],
                            f"{path} response {status}",
                        )


if __name__ == "__main__":
    unittest.main()
