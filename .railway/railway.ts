import { defineRailway, github, project, service, volume } from "railway/iac";
// Manage the API and its existing data volume; leave Temporal and Postgres intact.
export const partial = "Terra";
export default defineRailway(() => {
  const data = volume("terra-volume", { sizeMB: 500, region: "sfo" });
  const Terra = service("Terra", {
    source: github("Arnavnemade1/Terra", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile", watchPatterns: ["backend/**", "scripts/drone_bridge.py", "pyproject.toml", "Dockerfile", ".railway/**"] },
    start: "python -m terra.runtime",
    healthcheck: "/api/ready",
    healthcheckTimeout: 180,
    replicas: { "sfo": 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 5, overlapSeconds: 0, drainingSeconds: 30 },
    volumeMounts: { "/data": data },
    networking: { privateNetworkEndpoint: "terra" },
  });

  return project("hospitable-charisma", {
    variables: { managed: false },
    resources: [Terra, data],
  });
});
