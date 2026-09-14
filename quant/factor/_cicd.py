class CICDGenerator:
    """生成 GitHub Actions / GitLab CI 配置."""
    GITHUB_ACTIONS_TEMPLATE = """name: Factor CI/CD
on:
  push:
    branches: [main, develop]
    paths:
      - 'quant/factor/**'
      - 'tests/factor/**'
  pull_request:
    branches: [main, develop]
  workflow_dispatch:
    inputs:
      factor_name:
        description: 'Factor name to test'
        required: false
      stage:
        description: 'Stage to run (compile/test/backtest/register/deploy/all)'
        required: false
        default: 'all'
jobs:
  factor-ci:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install -e .
      - name: Run Factor Pipeline
        env:
          FACTOR_NAME: ${{ github.event.inputs.factor_name }}
          STAGE: ${{ github.event.inputs.stage }}
        run: |
          python -c "
from quant.factor.platform import FactorPipeline
pipeline = FactorPipeline()
result = pipeline.run('${{ env.FACTOR_NAME }}', stage='${{ env.STAGE }}')
import json
print(json.dumps([r.__dict__ for r in result], default=str))
          "
  deploy-staging:
    needs: factor-ci
    if: github.ref == 'refs/heads/main' && success()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to Staging
        run: |
          echo "Deploy to staging environment"
          # kubectl apply -f k8s/staging/
  deploy-production:
    needs: deploy-staging
    if: github.ref == 'refs/heads/main' && success()
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to Production
        run: |
          echo "Deploy to production"
          # kubectl apply -f k8s/production/
  notify:
    needs: [deploy-staging, deploy-production]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Notify
        run: |
          echo "Pipeline completed"
"""
    @classmethod
    def generate_github_actions(cls, output_dir: Path = Path(".github/workflows")):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "factor-ci.yml"
        path.write_text(cls.GITHUB_ACTIONS_TEMPLATE)
        _log.info(f"GitHub Actions workflow generated: {path}")
    @classmethod
    def generate_gitlab_ci(cls, output_path: Path = Path(".gitlab-ci.yml")):
        gitlab_ci = """
stages:
  - compile
  - test
  - backtest
  - register
  - deploy
variables:
  PYTHONPATH: "."
  PYTHON_VERSION: "3.12"
compile:
  stage: compile
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='compile'); print(r)"
  artifacts:
    reports:
      junit: test-results.xml
test:
  stage: test
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='test'); print(r)"
  coverage: '/Coverage: \d+\.\d+%/'
backtest:
  stage: backtest
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='backtest'); print(r)"
  when: manual
  allow_failure: true
register:
  stage: register
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='register'); print(r)"
  when: manual
  only:
    - main
deploy:
  stage: deploy
  script:
    - echo "Deploy to production"
  when: manual
  only:
    - tags
  environment:
    name: production
"""
        output_path.write_text(gitlab_ci)
        _log.info(f"GitLab CI generated: {output_path}")
# ── 全局实例 ──
_factor_registry: Optional[FactorRegistry] = None
_factor_pipeline: Optional[FactorPipeline] = None
def get_factor_registry() -> FactorRegistry:
    global _factor_registry
    if _factor_registry is None:
        _factor_registry = FactorRegistry()
    return _factor_registry
def get_factor_pipeline() -> FactorPipeline:
    global _factor_pipeline
    if _factor_pipeline is None:
        _factor_pipeline = FactorPipeline(get_factor_registry())
    return _factor_pipeline
# ── CLI 入口 ──
def main():
    """CLI 入口: factor-platform <command> [args]."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: factor-platform <command> [args]")
        print("Commands: register, test, pipeline, docs, ci-gen")
        return 1
    cmd = sys.argv[1]
    registry = get_factor_registry()
    pipeline = get_factor_pipeline()
    if cmd == "register":
        # factor-platform register <name> <expression> <source> <direction> <category> [--version]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("expression")
        parser.add_argument("source")
        parser.add_argument("direction", choices=["positive", "negative"])
        parser.add_argument("category", choices=[c.value for c in FactorCategory])
        parser.add_argument("--version", default="1.0")
        parser.add_argument("--source", default="internal")
        parser.add_argument("--author", default="auto")
        parser.add_argument("--description", default="")
        args = parser.parse_args(sys.argv[2:])
        meta = FactorMetadata(
            name=args.name, version=args.version, expression=args.expression,
            category=FactorCategory(args.category), source=args.source,
            author=args.author, direction=args.direction,
            description=args.description,
        )
        ok = get_factor_registry().register(meta)
        print("Registered" if ok else "Failed")
        return 0 if ok else 1
    elif cmd == "test":
        # factor-platform test <factor_name> [--version]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        args = parser.parse_args(sys.argv[2:])
        result = get_factor_pipeline().test_runner.run_tests(args.name, args.version)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["success"] else 1
    elif cmd == "pipeline":
        # factor-platform pipeline <factor_name> [--version] [--stages]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        parser.add_argument("--stages", nargs="+", default=None)
        args = parser.parse_args(sys.argv[2:])
        results = get_factor_pipeline().run(args.name, args.version, args.stages)
        for r in results:
            print(f"  {r.stage}: {r.status} ({r.duration_sec:.1f}s)")
            if r.error:
                print(f"  ERROR: {r.error}")
        return 0 if all(r.status == "success" for r in results) else 1
    elif cmd == "docs":
        # factor-platform docs <factor_name> [--version] [--output-dir]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        parser.add_argument("--output-dir", default="docs/factors")
        args = parser.parse_args(sys.argv[2:])
        path = get_factor_pipeline().generate_docs(args.name, args.version, args.output_dir)
        print(f"Documentation generated: {path}")
        return 0
    elif cmd == "ci-gen":
        # factor-platform ci-gen [--github|--gitlab] [--output-dir]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--github", action="store_true")
        parser.add_argument("--gitlab", action="store_true")
        parser.add_argument("--output-dir", default=".github/workflows")
        args = parser.parse_args(sys.argv[2:])
        if args.github:
            CICDGenerator.generate_github_actions(Path(args.output_dir))
        if args.gitlab:
            CICDGenerator.generate_gitlab_ci(Path(".gitlab-ci.yml"))
        print("CI/CD config generated")
        return 0
    else:
        print(f"Unknown command: {cmd}")
        return 1
if __name__ == "__main__":
    import sys
    sys.exit(main())

