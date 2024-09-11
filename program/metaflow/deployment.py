import logging
import os
import sys

from common import PYTHON, FlowMixin
from dotenv import load_dotenv

from metaflow import (
    FlowSpec,
    Parameter,
    environment,
    project,
    pypi_base,
    step,
)

logger = logging.getLogger(__name__)


@project(name="penguins")
@pypi_base(
    python=PYTHON,
    packages={
        "python-dotenv": "1.0.1",
        "mlflow": "2.16.0",
        "boto3": "1.35.8",
        "azure-ai-ml": "1.19.0",
        "azureml-mlflow": "1.57.0.post1",
    },
)
# @trigger_on_finish(flow="TrainingFlow")
class DeploymentFlow(FlowSpec, FlowMixin):
    """Deployment pipeline.

    This pipeline deploys the latest model from the model registry to a target platform
    and runs a few samples through the deployed model to ensure it's working.
    """

    endpoint = Parameter(
        "endpoint",
        help="The endpoint name that will be created in the target platform.",
        default="penguins",
    )

    target = Parameter(
        "target",
        help=(
            "The target platform where the pipeline will deploy the model. "
            "Currently, the supported targets are `sagemaker` and `azure`."
        ),
        default="sagemaker",
    )

    @environment(
        vars={"MLFLOW_TRACKING_URI": os.getenv("MLFLOW_TRACKING_URI")},
    )
    @step
    def start(self):
        """Start the deployment pipeline."""
        import mlflow
        from mlflow import MlflowClient

        self.mlflow_tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
        logging.info("MLflow tracking URI: %s", self.mlflow_tracking_uri)
        mlflow.set_tracking_uri(self.mlflow_tracking_uri)

        # We want to make sure that the specified target platform is supported by the
        # pipeline.
        if self.target not in ["sagemaker", "azure"]:
            message = (
                f'Target "{self.target}" is not supported. The supported targets are '
                "`sagemaker` and `azure`."
            )
            raise ValueError(message)

        self.data = self.load_dataset()

        # Let's connect to the model registry and find the latest model version
        # registered under the name "penguins".
        client = MlflowClient()
        response = client.search_model_versions(
            "name='penguins'",
            max_results=1,
            order_by=["last_updated_timestamp DESC"],
        )

        if not response:
            message = 'No model versions found registered under the name "penguins".'
            raise RuntimeError(message)

        self.latest_model = response[0]
        logger.info(
            "Model version: %s. Artifacts: %s.",
            self.latest_model.version,
            self.latest_model.source,
        )

        self.next(self.deployment)

    @step
    def deployment(self):
        """Deploy the model to the appropriate target platform."""
        import tempfile
        from pathlib import Path

        import mlflow

        mlflow.set_tracking_uri(self.mlflow_tracking_uri)

        # Let's download the model artifacts from the model registry to a temporary
        # directory. This is the copy that we'll use to deploy the model.
        with tempfile.TemporaryDirectory() as directory:
            mlflow.artifacts.download_artifacts(
                run_id=self.latest_model.run_id,
                dst_path=directory,
            )

            self.model_artifacts = f"file://{(Path(directory) / 'model').as_posix()}"
            logger.info("Model artifacts downloaded to %s ", self.model_artifacts)

            if self.target == "sagemaker":
                self._deploy_to_sagemaker()
            elif self.target == "azure":
                self._deploy_to_azure()

        self.next(self.inference)

    @step
    def inference(self):
        """Run a few samples through the deployed model to make sure it's working."""
        # Let's select a few random samples from the dataset.
        samples = self.data.sample(n=3).drop(columns=["species"]).reset_index(drop=True)

        if self.target == "sagemaker":
            self._run_sagemaker_prediction(samples)
        elif self.target == "azure":
            self._run_azure_prediction(samples)

        self.next(self.end)

    @step
    def end(self):
        """Finalize the deployment pipeline."""
        logger.info("The End")

    def _deploy_to_sagemaker(self):
        """Deploy the model to SageMaker.

        This function creates a new SageMaker model, endpoint configuration, and
        endpoint to serve the latest version of the model.

        If the endpoint already exists, this function will update it with the latest
        version of the model.
        """
        from mlflow.deployments import get_deploy_client
        from mlflow.exceptions import MlflowException

        # TODO: It should read default region
        # Let's start by getting the configuration to connect to SageMaker from
        # environment variables.
        # region = os.environ.get("SAGEMAKER_REGION")

        # if not region:
        #     message = (
        #         "Missing required environment variables. "
        #         "To deploy the model to SageMaker, you need to set the "
        #         "SAGEMAKER_REGION environment variable."
        #     )
        #     raise RuntimeError(message)

        deployment_configuration = {
            "instance_type": "ml.m4.xlarge",
            "instance_count": 1,
            "synchronous": True,
            # We want to archive resources associated with the endpoint that become
            # inactive as the result of updating an existing deployment.
            "archive": True,
            # Notice how we are storing the version number as a tag.
            "tags": {"version": self.latest_model.version},
        }

        self.deployment_target_uri = "sagemaker:/us-east-1/arn:aws:iam::325223348818:role/mlschool-MLSchoolRole-1icZiNTorrhb"  # TODO
        deployment_client = get_deploy_client(self.deployment_target_uri)

        try:
            # Let's return the deployment with the name of the endpoint we want to
            # create. If the endpoint doesn't exist, this function will raise an
            # exception.
            deployment = deployment_client.get_deployment(self.endpoint)

            # We now need to check whether the model we want to deploy is already
            # associated with the endpoint.
            if self._is_sagemaker_model_running(deployment):
                logger.info(
                    'Enpoint "%s" is already running model "%s".',
                    self.endpoint,
                    self.latest_model.version,
                )
            else:
                # If the model we want to deploy is not associated with the endpoint,
                # we need to update the current deployment to replace the previous model
                # with the new one.
                self._update_sagemaker_deployment(
                    deployment_client,
                    deployment_configuration,
                )
        except MlflowException:
            # If the endpoint doesn't exist, we can create a new deployment.
            self._create_sagemaker_deployment(
                deployment_client,
                deployment_configuration,
            )

    def _is_sagemaker_model_running(self, deployment):
        """Check if the model is already running in SageMaker.

        This function will check if the current model is already associated with a
        running SageMaker endpoint.
        """
        import boto3

        # TODO
        sts_client = boto3.client("sts")

        # Assume the role and get temporary credentials
        response = sts_client.assume_role(
            RoleArn="arn:aws:iam::325223348818:role/mlschool-MLSchoolRole-1icZiNTorrhb",
            RoleSessionName="mlschool-session",
        )

        # Extract the temporary credentials
        credentials = response["Credentials"]
        access_key = credentials["AccessKeyId"]
        secret_key = credentials["SecretAccessKey"]
        session_token = credentials["SessionToken"]

        # Step 2: Create a session with the assumed role credentials
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

        # Step 3: Use the session to create a SageMaker client
        sagemaker_client = session.client("sagemaker")

        # sagemaker_client = boto3.client("sagemaker")

        # Here, we're assuming there's only one production variant associated with
        # the endpoint. This code will need to be updated if an endpoint could have
        # multiple variants.
        variant = deployment.get("ProductionVariants", [])[0]

        # From the variant, we can get the ARN of the model associated with the
        # endpoint.
        model_arn = sagemaker_client.describe_model(
            ModelName=variant.get("VariantName"),
        ).get("ModelArn")

        # With the model ARN, we can get the tags associated with the model.
        tags = sagemaker_client.list_tags(ResourceArn=model_arn).get("Tags", [])

        # Finally, we can check whether the model has a `version` tag that matches
        # the model version we're trying to deploy.
        model = next(
            (
                tag["Value"]
                for tag in tags
                if (
                    tag["Key"] == "version"
                    and tag["Value"] == self.latest_model.version
                )
            ),
            None,
        )

        return model is not None

    def _create_sagemaker_deployment(self, deployment_client, deployment_configuration):
        """Create a new SageMaker deployment using the supplied configuration."""
        logger.info(
            'Creating endpoint "%s" with model "%s"...',
            self.endpoint,
            self.latest_model.version,
        )

        deployment_client.create_deployment(
            name=self.endpoint,
            model_uri=self.model_artifacts,
            flavor="python_function",
            config=deployment_configuration,
        )

    def _update_sagemaker_deployment(self, deployment_client, deployment_configuration):
        """Update an existing SageMaker deployment using the supplied configuration."""
        logger.info(
            'Updating endpoint "%s" with model "%s"...',
            self.endpoint,
            self.latest_model.version,
        )

        # If you wanted to implement a staged rollout, you could extend the deployment
        # configuration with a `mode` parameter with the value
        # `mlflow.sagemaker.DEPLOYMENT_MODE_ADD` to create a new production variant. You
        # can then route some of the traffic to the new variant using the SageMaker SDK.
        deployment_client.update_deployment(
            name=self.endpoint,
            model_uri=self.model_artifacts,
            flavor="python_function",
            config=deployment_configuration,
        )

    def _run_sagemaker_prediction(self, samples):
        import pandas as pd
        from mlflow.deployments import get_deploy_client

        deployment_client = get_deploy_client(self.deployment_target_uri)

        logger.info('Running prediction on "%s"...', self.endpoint)
        response = deployment_client.predict(self.endpoint, samples)
        df = pd.DataFrame(response["predictions"])[["prediction", "confidence"]]

        logger.info("\n%s", df)

    def _deploy_to_azure(self):
        """Deploy the model to Azure ML.

        This function creates a new Azure model, endpoint, and deployment to serve the
        latest version of the model.

        If the endpoint already exists and there's an active deployment associated
        with it, this function will create a new deployment, route 100% of the traffic
        to it, and delete the previous deployment.
        """
        import os

        import mlflow
        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential

        # Let's start by getting the configuration to connect to Azure from
        # environment variables.
        subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
        resource_group = os.environ.get("AZURE_RESOURCE_GROUP")
        workspace = os.environ.get("AZURE_WORKSPACE")

        if not all([subscription_id, resource_group, workspace]):
            message = (
                "Missing required environment variables. "
                "To deploy the model to Azure, you need to set the "
                "AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, and AZURE_WORKSPACE "
                "environment variables."
            )
            raise RuntimeError(message)

        # Let's connect to Azure and get the tracking URI that we need to configure
        # MLflow to use the Azure ML workspace.
        ml_client = MLClient(
            DefaultAzureCredential(),
            subscription_id,
            resource_group,
            workspace,
        )

        self.deployment_target_uri = ml_client.workspaces.get(
            ml_client.workspace_name,
        ).mlflow_tracking_uri

        mlflow.set_tracking_uri(self.deployment_target_uri)

        model = self._create_azure_model()
        self._create_azure_endpoint()
        self._create_azure_deployment(model)

    def _create_azure_model(self):
        """Create an Azure model if it doesn't exist.

        The first step to deploy a model to Azure is to register it. This function will
        register the model version if it doesn't exist. Azure will automatically assign
        a new version number to the model, so we'll keep the original version number as
        a tag to keep track of it.
        """
        from mlflow import MlflowClient

        model_name = "penguins"

        # Let's connect to Azure and return every model that matches the name we're
        # going to use to register the model.
        mlflow_client = MlflowClient(self.deployment_target_uri)
        models = mlflow_client.search_model_versions(
            filter_string=f"name = '{model_name}'",
        )

        # If we find any matching models, we need to check whether the latest version
        # we're going to use is already registered. Notice how we're using the version
        # number stored as a tag.
        model = next(
            (
                m
                for m in models
                if int(m.tags.get("version", 0)) == self.latest_model.version
            ),
            None,
        )

        if model:
            logger.info('Model "%s" already exists.', model_name)
            return model

        # If we don't find a model that matches the latest version, we can register
        # the model in Azure.
        logger.info('Creating model "%s"...', model_name)
        return mlflow_client.create_model_version(
            name=model_name,
            source=self.model_artifacts,
            # We want to store the model version as a tag.
            tags={"version": self.latest_model.version},
        )

    def _create_azure_endpoint(self):
        """Create an Azure endpoint if it doesn't exist.

        An endpoint is the entry point that clients will use for online (real-time)
        inferencing. This function will create the endpoint if it doesn't exist.
        """
        from azure.core.exceptions import ResourceNotFoundError
        from mlflow.deployments import get_deploy_client

        deployment_client = get_deploy_client(self.deployment_target_uri)

        try:
            # Let's try to get the endpoint. If it doesn't exist, this function will
            # raise an exception.
            deployment_client.get_endpoint(self.endpoint)
            logger.info('Endpoint "%s" already exists.', self.endpoint)
        except ResourceNotFoundError:
            logger.info('Creating endpoint "%s"...', self.endpoint)
            deployment_client.create_endpoint(self.endpoint)

    def _create_azure_deployment(self, model):
        """Create an Azure deployment if it doesn't exist.

        A deployment is the set of resources required for hosting the model behind an
        endpoint. This function will create a new deployment if it doesn't exist, route
        all traffic to it, and delete the previous deployment.
        """
        import json
        import tempfile

        from mlflow.deployments import get_deploy_client

        # Let's setup the name of the deployment we want to create. We want to store
        # this name as an artifact of the flow to use it later to make predictions.
        self.deployment_name = f"{self.endpoint}-{self.latest_model.version}"

        deployment_client = get_deploy_client(self.deployment_target_uri)

        # Let's get the list of deployments associated with the endpoint.
        deployments = deployment_client.list_deployments(self.endpoint)

        # We don't want to do anything if the deployment already exists, so let's
        # display a message and leave.
        if any(d["name"] == self.deployment_name for d in deployments):
            logger.info('Deployment "%s" already exists.', self.deployment_name)
            return

        # If we need to create a new deployment, let's store the name of the current
        # deployment so we can delete it later.
        previous_deployment = deployments[0]["name"] if len(deployments) > 0 else None

        # To configure the deployment and its traffic, we need to create two temporary
        # configuration files with the settings we want to give to Azure. I don't like
        # this but this is how their SDK works.
        with (
            tempfile.NamedTemporaryFile(mode="w") as deployment_config,
            tempfile.NamedTemporaryFile(mode="w") as traffic_config,
        ):
            # We are going to use a single instance to host the model.
            json.dump(
                {
                    "instance_type": "Standard_DS3_v2",
                    "instance_count": 1,
                },
                deployment_config,
            )

            # We want to route 100% of the traffic to the new deployment. If you wanted
            # to implement a staged rollout, you would configure the traffic
            # distribution between deployments here.
            json.dump(
                {
                    "traffic": {
                        self.deployment_name: 100,
                    },
                },
                traffic_config,
            )

            # Let's flush the configuration files to disk so we can use them.
            deployment_config.flush()
            traffic_config.flush()

            # Now we can create the new deployment using the current model.
            logger.info('Creating new deployment "%s"...', self.deployment_name)
            deployment_client.create_deployment(
                name=self.deployment_name,
                endpoint=self.endpoint,
                model_uri=f"models:/{model.name}/{model.version}",
                config={"deploy-config-file": deployment_config.name},
            )

            # After creating the deployment, we need to update the traffic distribution
            # to route all traffic to it.
            logger.info("Updating endpoint traffic...")
            deployment_client.update_endpoint(
                endpoint=self.endpoint,
                config={"endpoint-config-file": traffic_config.name},
            )

            # Finally, if there was a previous active deployment, we need to delete it.
            if previous_deployment:
                logger.info('Deleting previous deployment "%s"...', previous_deployment)
                deployment_client.delete_deployment(
                    name=previous_deployment,
                    endpoint=self.endpoint,
                )

    def _run_azure_prediction(self, samples):
        from mlflow.deployments import get_deploy_client

        deployment_client = get_deploy_client(self.deployment_target_uri)

        logger.info(
            'Running prediction on "%s/%s"...',
            self.endpoint,
            self.deployment_name,
        )

        response = deployment_client.predict(
            endpoint=self.endpoint,
            deployment_name=self.deployment_name,
            df=samples,
        )

        logger.info("\n%s", response)


if __name__ == "__main__":
    load_dotenv()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        level=logging.INFO,
    )
    logging.getLogger("mlflow.sagemaker").setLevel(logging.ERROR)
    logging.getLogger("botocore.credentials").setLevel(logging.ERROR)

    DeploymentFlow()
