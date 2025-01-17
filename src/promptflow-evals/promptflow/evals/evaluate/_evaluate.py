# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------
import inspect
import re
from typing import Any, Callable, Dict, Optional, Set, Tuple

import pandas as pd

from promptflow._sdk._constants import LINE_NUMBER
from promptflow.client import PFClient
from ._utils import _log_metrics_and_instance_results, _trace_destination_from_project_scope, _write_output
from .._user_agent import USER_AGENT


def _calculate_mean(df) -> Dict[str, float]:
    df.rename(columns={col: col.replace("outputs.", "") for col in df.columns}, inplace=True)
    mean_value = df.mean(numeric_only=True)
    return mean_value.to_dict()


def _validate_input_data_for_evaluator(evaluator, evaluator_name, df_data, is_target_fn=False):
    required_inputs = [
        param.name
        for param in inspect.signature(evaluator).parameters.values()
        if param.default == inspect.Parameter.empty and param.name not in ["kwargs", "args", "self"]
    ]

    missing_inputs = [col for col in required_inputs if col not in df_data.columns]
    if missing_inputs:
        if not is_target_fn:
            raise ValueError(f"Missing required inputs for evaluator {evaluator_name} : {missing_inputs}.")
        else:
            raise ValueError(f"Missing required inputs for target : {missing_inputs}.")


def _validate_and_load_data(target, data, evaluators, output_path, azure_ai_project, evaluation_name):
    if data is None:
        raise ValueError("data must be provided for evaluation.")

    if target is not None:
        if not callable(target):
            raise ValueError("target must be a callable function.")

    if data is not None:
        if not isinstance(data, str):
            raise ValueError("data must be a string.")

    if evaluators is not None:
        if not isinstance(evaluators, dict):
            raise ValueError("evaluators must be a dictionary.")

    if output_path is not None:
        if not isinstance(output_path, str):
            raise ValueError("output_path must be a string.")

    if azure_ai_project is not None:
        if not isinstance(azure_ai_project, Dict):
            raise ValueError("azure_ai_project must be a Dict.")

    if evaluation_name is not None:
        if not isinstance(evaluation_name, str):
            raise ValueError("evaluation_name must be a string.")

    try:
        initial_data_df = pd.read_json(data, lines=True)
    except Exception as e:
        raise ValueError(f"Failed to load data from {data}. Please validate it is a valid jsonl data. Error: {str(e)}.")

    return initial_data_df


def _validate_columns(
    df: pd.DataFrame,
    evaluators: Dict[str, Any],
    target: Optional[Callable],
    evaluator_config: Dict[str, Dict[str, str]],
) -> None:
    """
    Check that all columns needed by evaluator or target function are present.

    :keyword df: The data frame to be validated.
    :paramtype df: pd.DataFrame
    :keyword evaluators: The dictionary of evaluators.
    :paramtype evaluators: Dict[str, Any]
    :keyword target: The callable to be applied to data set.
    :paramtype target: Optional[Callable]
    """
    if target:
        # If the target function is given, it may return
        # several columns and hence we cannot check the availability of columns
        # without knowing target function semantics.
        # Instead, here we will validate the columns, taken by target.
        _validate_input_data_for_evaluator(target, None, df, is_target_fn=True)
    else:
        for evaluator_name, evaluator in evaluators.items():
            # Apply column mapping
            mapping_config = evaluator_config.get(evaluator_name, evaluator_config.get("default", None))
            new_df = _apply_column_mapping(df, mapping_config)

            # Validate input data for evaluator
            _validate_input_data_for_evaluator(evaluator, evaluator_name, new_df)


def _apply_target_to_data(
    target: Callable, data: str, pf_client: PFClient, initial_data: pd.DataFrame,
    evaluation_name: Optional[str] = None
) -> Tuple[pd.DataFrame, Set[str]]:
    """
    Apply the target function to the data set and return updated data and generated columns.

    :keyword target: The function to be applied to data.
    :paramtype target: Callable
    :keyword data: The path to input jsonl file.
    :paramtype data: str
    :keyword pf_client: The promptflow client to be used.
    :paramtype pf_client: PFClient
    :keyword initial_data: The data frame with the loaded data.
    :paramtype initial_data: pd.DataFrame
    :return: The tuple, containing data frame and the list of added columns.
    :rtype: Tuple[pd.DataFrame, List[str]]
    """
    # We are manually creating the temporary directory for the flow
    # because the way tempdir remove temporary directories will
    # hang the debugger, because promptflow will keep flow directory.
    run = pf_client.run(
        flow=target,
        display_name=evaluation_name,
        data=data,
        properties={
            "runType": "eval_run",
            "isEvaluatorRun": "true"
        },
        stream=True
    )
    target_output = pf_client.runs.get_details(run, all_results=True)
    # Remove input and output prefix
    prefix = "outputs."
    rename_dict = {col: col[len(prefix):] for col in target_output.columns if col.startswith(prefix)}
    # Sometimes user data may contain column named the same as the one generated by target.
    # In this case we will not rename the column.
    generated_columns = set(rename_dict.values())
    for col in initial_data.columns:
        if col in generated_columns:
            tgt_out = f'{prefix}{col}'
            del rename_dict[tgt_out]
    # Sort output by line numbers
    target_output.set_index(f"inputs.{LINE_NUMBER}", inplace=True)
    target_output.sort_index(inplace=True)
    target_output.reset_index(inplace=True, drop=False)
    # target_output contains only input columns, taken by function,
    # so we need to concatenate it to the input data frame.
    drop_columns = list(filter(lambda x: x.startswith('inputs'), target_output.columns))
    target_output.drop(drop_columns, inplace=True, axis=1)
    # Remove outputs. prefix
    target_output.rename(columns=rename_dict, inplace=True)
    # Concatenate output to input
    target_output = pd.concat([target_output, initial_data], axis=1)
    return target_output, generated_columns, run


def _apply_column_mapping(
        source_df: pd.DataFrame, mapping_config: dict, inplace: bool = False) -> pd.DataFrame:
    """
    Apply column mapping to source_df based on mapping_config.

    This function is used for pre-validation of input data for evaluators
    :param source_df: the data frame to be changed.
    :type source_df: pd.DataFrame
    :param mapping_config: The configuration, containing column mapping.
    :type mapping_config: dict.
    :param inplace: If true, the source_df will be changed inplace.
    :type inplace: bool
    :return: The modified data frame.
    """
    result_df = source_df

    if mapping_config:
        column_mapping = {}
        columns_to_drop = set()
        pattern_prefix = "data."
        run_outputs_prefix = "run.outputs."

        for map_to_key, map_value in mapping_config.items():
            match = re.search(r"^\${([^{}]+)}$", map_value)
            if match is not None:
                pattern = match.group(1)
                if pattern.startswith(pattern_prefix):
                    column_mapping[pattern[len(pattern_prefix):]] = map_to_key
                elif pattern.startswith(run_outputs_prefix):
                    map_from_key = pattern[len(run_outputs_prefix):]
                    col_outputs = f'outputs.{map_from_key}'
                    # If data set had target-generated column before application of
                    # target, the column will have "outputs." prefix. We will use
                    # target-generated column for validation.
                    if col_outputs in source_df.columns:
                        map_from_key = col_outputs

                    # If column needs to be mapped to already existing column.
                    if map_to_key in source_df.columns:
                        columns_to_drop.add(map_to_key)
                    column_mapping[map_from_key] = map_to_key
        # If some columns, which has to be dropped actually can renamed,
        # we will not drop it.
        columns_to_drop = columns_to_drop - set(column_mapping.keys())
        result_df = source_df.drop(columns=columns_to_drop, inplace=inplace)
        result_df.rename(columns=column_mapping, inplace=True)

    return result_df


def _process_evaluator_config(evaluator_config: Dict[str, Dict[str, str]]):
    """Process evaluator_config to replace ${target.} with ${data.}"""

    processed_config = {}

    unexpected_references = re.compile(r"\${(?!target\.|data\.).+?}")

    if evaluator_config:
        for evaluator, mapping_config in evaluator_config.items():
            if isinstance(mapping_config, dict):
                processed_config[evaluator] = {}

                for map_to_key, map_value in mapping_config.items():

                    # Check if there's any unexpected reference other than ${target.} or ${data.}
                    if unexpected_references.search(map_value):
                        raise ValueError(
                            "Unexpected references detected in 'evaluator_config'. "
                            "Ensure only ${target.} and ${data.} are used."
                        )

                    # Replace ${target.} with ${run.outputs.}
                    processed_config[evaluator][map_to_key] = map_value.replace("${target.", "${run.outputs.")

    return processed_config


def _rename_columns_conditionally(df: pd.DataFrame, target_generated: Set[str]):
    """
    Change the column names for data frame. The change happens inplace.

    The columns with "outputs." prefix will not be changed. "outputs." prefix will
    will be added to columns in target_generated set. The rest columns will get
    ".inputs" prefix.
    :param df: The data frame to apply changes to.
    :param target_generated: The columns generated by the target.
    :return: The changed data frame.
    """
    rename_dict = {}
    for col in df.columns:
        outputs_col = f'outputs.{col}'
        # Do not rename columns with "outputs."
        if 'outputs.' in col and col[len('outputs.'):] in target_generated:
            continue
        # If target has generated outputs.{col}, we do not need to rename column
        # as it is actually input. Otherwise add outputs. prefix.
        if col in target_generated and outputs_col not in df.columns:
            rename_dict[col] = outputs_col
        else:
            rename_dict[col] = f'inputs.{col}'
    df.rename(columns=rename_dict, inplace=True)
    return df


def evaluate(
    *,
    evaluation_name: Optional[str] = None,
    target: Optional[Callable] = None,
    data: Optional[str] = None,
    evaluators: Optional[Dict[str, Callable]] = None,
    evaluator_config: Optional[Dict[str, Dict[str, str]]] = {},
    azure_ai_project: Optional[Dict] = None,
    output_path: Optional[str] = None,
    **kwargs,
):
    """Evaluates target or data with built-in evaluation metrics

    :keyword evaluation_name: Display name of the evaluation.
    :paramtype evaluation_name: Optional[str]
    :keyword target: Target to be evaluated. `target` and `data` both cannot be None
    :paramtype target: Optional[Callable]
    :keyword data: Path to the data to be evaluated or passed to target if target is set.
        Only .jsonl format files are supported.  `target` and `data` both cannot be None
    :paramtype data: Optional[str]
    :keyword evaluator_config: Configuration for evaluators.
    :paramtype evaluator_config: Optional[Dict[str, Dict[str, str]]
    :keyword output_path: The local folder path to save evaluation artifacts to if set
    :paramtype output_path: Optional[str]
    :keyword azure_ai_project: Logs evaluation results to AI Studio
    :paramtype azure_ai_project: Optional[Dict]
    :return: A EvaluationResult object.
    :rtype: ~azure.ai.generative.evaluate.EvaluationResult
    """

    trace_destination = _trace_destination_from_project_scope(azure_ai_project) if azure_ai_project else None

    input_data_df = _validate_and_load_data(target, data, evaluators, output_path, azure_ai_project, evaluation_name)

    # Process evaluator config to replace ${target.} with ${data.}
    evaluator_config = _process_evaluator_config(evaluator_config)
    _validate_columns(input_data_df, evaluators, target, evaluator_config)

    pf_client = PFClient(
        config={
            "trace.destination": trace_destination
        },
        user_agent=USER_AGENT,

    )
    target_run = None

    target_generated_columns = set()
    if data is not None and target is not None:
        input_data_df, target_generated_columns, target_run = _apply_target_to_data(target, data, pf_client,
                                                                                    input_data_df, evaluation_name)

        # Make sure, the default is always in the configuration.
        if not evaluator_config:
            evaluator_config = {}
        if 'default' not in evaluator_config:
            evaluator_config['default'] = {}

        for evaluator_name, mapping in evaluator_config.items():
            mapped_to_values = set(mapping.values())
            for col in target_generated_columns:
                # If user defined mapping differently, do not change it.
                # If it was mapped to target, we have already changed it
                # in _process_evaluator_config
                run_output = f'${{run.outputs.{col}}}'
                # We will add our mapping only if
                # customer did not mapped target output.
                if col not in mapping and run_output not in mapped_to_values:
                    evaluator_config[evaluator_name][col] = run_output

        # After we have generated all columns we can check if we have
        # everything we need for evaluators.
        _validate_columns(input_data_df, evaluators, target=None, evaluator_config=evaluator_config)

    evaluator_info = {}

    for evaluator_name, evaluator in evaluators.items():
        evaluator_info[evaluator_name] = {}
        evaluator_info[evaluator_name]["run"] = pf_client.run(
            flow=evaluator,
            run=target_run,
            column_mapping=evaluator_config.get(evaluator_name, evaluator_config.get("default", None)),
            data=data,
            stream=True,
        )

    evaluators_result_df = None
    for evaluator_name, evaluator_info in evaluator_info.items():
        evaluator_result_df = pf_client.get_details(evaluator_info["run"], all_results=True)

        # drop input columns
        evaluator_result_df = evaluator_result_df.drop(
            columns=[col for col in evaluator_result_df.columns if col.startswith("inputs.")]
        )

        # rename output columns
        # Assuming after removing inputs columns, all columns are output columns
        evaluator_result_df.rename(
            columns={
                col: "outputs." f"{evaluator_name}.{col.replace('outputs.', '')}"
                for col in evaluator_result_df.columns
            },
            inplace=True,
        )

        evaluators_result_df = (
            pd.concat([evaluators_result_df, evaluator_result_df], axis=1, verify_integrity=True)
            if evaluators_result_df is not None
            else evaluator_result_df
        )

    # Rename columns, generated by template function to outputs instead of inputs.
    # If target generates columns, already present in the input data, these columns
    # will be marked as outputs already so we do not need to rename them.
    input_data_df = _rename_columns_conditionally(input_data_df, target_generated_columns)

    result_df = pd.concat([input_data_df, evaluators_result_df], axis=1, verify_integrity=True)
    metrics = _calculate_mean(evaluators_result_df)

    studio_url = _log_metrics_and_instance_results(
        metrics, result_df, trace_destination, target_run, pf_client, data, evaluation_name)

    result = {"rows": result_df.to_dict("records"), "metrics": metrics, "studio_url": studio_url}

    if output_path:
        _write_output(output_path, result)

    return result
