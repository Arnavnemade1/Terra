import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "Terra";
export default defineRailway(() => {
  const Terra = service("Terra", {
    source: github("Arnavnemade1/Terra", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile", watchPatterns: ["backend/**", "pyproject.toml", "Dockerfile", "railway.json"] },
    start: "python -m terra.runtime",
    healthcheck: "/api/ready",
    healthcheckTimeout: 180,
    replicas: { "sfo": 1 },
    networking: { privateNetworkEndpoint: "terra" },
  });

  return project("hospitable-charisma", {
    variables: { managed: false },
    resources: [Terra],
  });
});
