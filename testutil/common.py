from itertools import chain
from typing import List, Tuple, Dict, Set

from pyspark.ml import Model
from pyspark.ml.classification import RandomForestClassifier, DecisionTreeClassifier, GBTClassifier
from pyspark.ml.feature import StringIndexer, VectorAssembler, SQLTransformer, OneHotEncoder
from pyspark.ml.regression import RandomForestRegressor, GBTRegressor, DecisionTreeRegressor
from pyspark.sql import SparkSession, DataFrame
import re
import pyspark.sql.functions as F

from transparency.spark.ohe.decoder import OneHotDecoder
from transparency.spark.prediction.explainer.tree import EnsembleTreeExplainTransformer


def get_ensemble_pipeline_stages(categorical_columns, continuous_columns, label_column, ensemble_type,
                                 classification=False
                                 ) -> List:
    encoders = []
    for c in categorical_columns:
        indexer = StringIndexer(inputCol=c, outputCol=f"{c}_IDX")
        encoders.append(indexer)
        encoder = OneHotEncoder(inputCol=indexer.getOutputCol(), outputCol=f"{c}_OHE", dropLast=False)
        encoders.append(encoder)

    features_column = f"features_{label_column}"
    prediction_column = f"prediction_{label_column}"

    assembler = VectorAssembler(inputCols=[f"{c}_OHE" for c in categorical_columns] + continuous_columns,
                                outputCol=features_column)

    if classification:
        _model = get_classifier(ensemble_type, label_column, features_column, prediction_column)
    else:
        _model = get_predictor(ensemble_type, label_column, features_column, prediction_column)

    stages: List = encoders + [assembler, _model]

    return stages


def get_classifier(ensemble_type, label_column, features_column, prediction_column):
    models = {
        'dct': DecisionTreeClassifier(labelCol=label_column, featuresCol=features_column,
                                      predictionCol=prediction_column),
        'gbt': GBTClassifier(labelCol=label_column, featuresCol=features_column,
                             predictionCol=prediction_column),
        'rf': RandomForestClassifier(labelCol=label_column, featuresCol=features_column,
                                     predictionCol=prediction_column),
    }
    return models.get(ensemble_type)


def get_predictor(ensemble_type, label_column, features_column, prediction_column):
    models = {
        'dct': DecisionTreeRegressor(labelCol=label_column, featuresCol=features_column,
                                     predictionCol=prediction_column),
        'gbt': GBTRegressor(labelCol=label_column, featuresCol=features_column,
                            predictionCol=prediction_column),
        'rf': RandomForestRegressor(labelCol=label_column, featuresCol=features_column,
                                    predictionCol=prediction_column),
    }
    return models.get(ensemble_type)


def get_feature_importance(spark_session: SparkSession, model: Model, prediction_df: DataFrame,
                           feature_column: str, ) -> DataFrame:
    values1 = prediction_df.schema[feature_column].metadata["ml_attr"]["attrs"].values()
    attrs = sorted((attr["idx"], attr["name"]) for attr in (chain(*values1)))
    feature_importance = [
        (idx, re.sub('[^0-9a-zA-Z]+', '_', name), name, float(model.featureImportances[idx]))
        for
        idx, name
        in attrs]
    # sort (feature,score) tuples in descending order of feature importance value
    feature_importance.sort(key=lambda x: x[3], reverse=True)
    feature_importance_df: DataFrame = spark_session.createDataFrame(feature_importance).toDF("Feature_Index",
                                                                                              "Feature",
                                                                                              "Original_Feature",
                                                                                              "Importance")

    return feature_importance_df


def get_ensemble_explain_stages(predictions_view: str, features_importance_view: str, label_column: str,
                                rf_model_path: str, ensemble_type: str, classification=False) -> List:
    stages = [
        OneHotDecoder(oheSuffix="_OHE", idxSuffix="_IDX", unknownSuffix="Unknown"),
        SQLTransformer(statement=f"CREATE OR REPLACE TEMPORARY VIEW {predictions_view} AS SELECT * from __THIS__"),
        EnsembleTreeExplainTransformer(predictionView=predictions_view, featureImportanceView=features_importance_view,
                                       modelPath=rf_model_path, label=label_column,
                                       dropPathColumn=True, isClassification=classification, ensembleType=ensemble_type)
    ]
    return stages


def get_dummy_encoded(source_df: DataFrame, categorical_columns: List[str]) -> Tuple[DataFrame, List[str]]:
    categorical_columns_distinct: Dict = dict()
    categorical_columns_encoded: Set = set()
    for column_name in categorical_columns:
        categorical_columns_distinct[column_name] = get_distinct_values(source_df, column_name)

    expressions = []
    for column_name, categories in categorical_columns_distinct.items():
        for cat in categories:
            expressions.append(F.when(F.col(column_name) == cat, 1).otherwise(0).alias(f"{column_name}_{cat}"))
            categorical_columns_encoded.add(f"{column_name}_{cat}")

    dummy_df = source_df.select(source_df.columns + expressions)
    return dummy_df, list(categorical_columns_encoded)


def get_distinct_values(source_df: DataFrame, column_name: str) -> List:
    return [row[column_name] for row in source_df.select(column_name).distinct().collect()]
