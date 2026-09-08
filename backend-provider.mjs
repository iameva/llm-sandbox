// Register a provider for this process without changing the harness's config or sessions.
export default function (api) {
  const config = JSON.parse(process.env.SANDBOX_PROVIDER_CONFIG);
  api.registerProvider('sandbox_backend', config);
}
