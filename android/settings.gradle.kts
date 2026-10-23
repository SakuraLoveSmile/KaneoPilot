pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
check(System.getenv("GITHUB_ACTIONS") == "true" && System.getenv("RUNNER_ENVIRONMENT") == "github-hosted") {
    "KaneoPilot builds run only in GitHub-hosted Actions. Use Packages: https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml"
}
dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }
rootProject.name = "KaneoPilot"
include(":app")
