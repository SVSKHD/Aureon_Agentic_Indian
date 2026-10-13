// dhan_bridge CI/CD - declarative pipeline.
//
// Agent requirements: python3.11, docker (with access to the daemon that builds
// the image), git, ssh/scp, curl. gitleaks and trivy run as pinned containers.
// Tests need real PostgreSQL: they start throwaway postgres:<major>-alpine containers
// through the same docker daemon (or use TEST_PG_ADMIN_URL if a sidecar is provided).
// Jenkins plugins: Pipeline, Git, SSH Agent, Credentials Binding, JUnit, Coverage,
// Timestamper, and Basic Branch Build Strategies (to build tags; see README).
//
// Credentials (by id only): registry-creds (username/password), staging-ssh and
// prod-ssh (SSH private keys), discord-ci-webhook (secret text).
// Jenkins NEVER handles Dhan secrets: they live only on the deploy hosts.
//
// Build once, promote: main builds, scans, smoke-tests and pushes
// <registry>/dhan_bridge:<GIT_SHA> (staging deploys that). A release tag vX.Y.Z
// does NOT rebuild: it pulls :<GIT_SHA> (failing if this commit was never built on
// main), re-runs lint/test/safety/scan/smoke against it, retags it :<VERSION> and
// pushes that. Prod therefore runs the exact bytes staging ran.
//
// Executors: the pipeline is `agent none`. The CI stages share one agent; the prod
// approval waits with NO executor held; the prod deploy then takes a fresh agent.

import groovy.json.JsonOutput

def mark(String name) {
    env.STAGES_REACHED = ((env.STAGES_REACHED ?: '') + "|${name}")
}

def notifyDiscord() {
    def result = currentBuild.currentResult
    def reached = (env.STAGES_REACHED ?: '').tokenize('|')
    def lines = []
    for (int i = 0; i < reached.size(); i++) {
        def failedHere = (i == reached.size() - 1) && result != 'SUCCESS'
        lines << "${failedHere ? ':x:' : ':white_check_mark:'} ${reached[i]}"
    }
    def icon = result == 'SUCCESS' ? ':green_circle:' : (result == 'ABORTED' ? ':white_circle:' : ':red_circle:')
    def msg = """${icon} **dhan_bridge ${env.VERSION ?: '?'}** (`${env.GIT_SHA ?: '?'}`) - **${result}**
Ref: ${env.TAG_NAME ?: env.BRANCH_NAME ?: '?'} | Target: ${env.DEPLOY_TARGET ?: 'none'}${env.APPROVER ? " | Approved by: ${env.APPROVER}" : ''}
${env.ROLLBACK ? ":rewind: Rollback: ${env.ROLLBACK}\n" : ''}${currentBuild.description ? "Note: ${currentBuild.description}\n" : ''}Stages:
${lines.join('\n')}
${env.BUILD_URL ?: ''}"""
    writeFile file: '.discord-payload.json', text: JsonOutput.toJson([username: 'dhan_bridge CI', content: (msg.length() > 1900 ? msg.substring(0, 1900) : msg)])
    withCredentials([string(credentialsId: 'discord-ci-webhook', variable: 'DISCORD_WEBHOOK')]) {
        sh '''curl -fsS -m 15 -H "Content-Type: application/json" -d @.discord-payload.json "$DISCORD_WEBHOOK" \
              || echo "WARNING: Discord notification failed"'''
    }
}

pipeline {
    agent none

    parameters {
        booleanParam(name: 'FORCE_MARKET_HOURS_DEPLOY', defaultValue: false,
                     description: 'Allow a PROD deploy between 09:00 and 15:45 IST on a trading day. Requires OVERRIDE_REASON.')
        string(name: 'OVERRIDE_REASON', defaultValue: '',
               description: 'Mandatory justification when FORCE_MARKET_HOURS_DEPLOY is set. Recorded in the build description.')
    }

    options {
        timestamps()
        skipDefaultCheckout()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '50', artifactNumToKeepStr: '20'))
    }

    environment {
        // TODO(placeholder): set the real registry and hosts.
        REGISTRY      = 'registry.example.com/aureon'
        IMAGE         = "${REGISTRY}/dhan_bridge"
        STAGING_HOST  = 'deploy@staging.example.com'
        PROD_HOST     = 'deploy@prod.example.com'
        REMOTE_DIR    = '/opt/dhan_bridge'
        // Host keys must already be in the agent's known_hosts (see README).
        SSH_OPTS      = '-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15'
        GITLEAKS_IMG  = 'zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f'
        TRIVY_IMG     = 'aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa'
        PY            = '.venv/bin/python'
        // Throwaway servers for tests / smoke (formatted with the server major).
        PG_TEST_IMAGE = 'postgres:{major}-alpine'
    }

    stages {
        stage('CI') {
            agent any
            stages {
                stage('Checkout') {
                    steps {
                        checkout scm
                        script {
                            mark('Checkout')
                            env.VERSION = sh(returnStdout: true,
                                script: '''sed -n 's/^__version__ = "\\(.*\\)"$/\\1/p' dhan_bridge/config.py''').trim()
                            env.GIT_SHA = sh(returnStdout: true, script: 'git rev-parse --short=12 HEAD').trim()
                            if (!env.VERSION) {
                                error('Could not read __version__ from dhan_bridge/config.py')
                            }
                            if (sh(returnStatus: true, script: 'grep -qF "## [${VERSION}]" CHANGELOG.md') != 0) {
                                error("CHANGELOG.md has no '## [${env.VERSION}]' entry")
                            }
                            if (env.TAG_NAME && env.TAG_NAME.startsWith('v') && env.TAG_NAME != "v${env.VERSION}") {
                                error("Tag ${env.TAG_NAME} does not match __version__ ${env.VERSION}")
                            }
                            currentBuild.displayName = "#${env.BUILD_NUMBER} ${env.VERSION} ${env.GIT_SHA}"
                        }
                        sh '''
                            rm -rf reports && mkdir -p reports
                            python3.11 -m venv .venv
                            .venv/bin/pip install --quiet --disable-pip-version-check --require-hashes \
                                -r requirements.txt -r requirements-dev.txt
                        '''
                    }
                }

                stage('Lint') {
                    steps {
                        mark('Lint')
                        sh '''
                            $PY -m ruff check .
                            $PY -m ruff format --check .
                            $PY -m mypy
                            $PY -m mypy --strict --follow-imports=silent dhan_bridge/calendar.py dhan_bridge/config.py
                        '''
                    }
                }

                stage('Test') {
                    steps {
                        mark('Test')
                        // Real PostgreSQL only (no SQLite fallback): tests start their own
                        // throwaway postgres containers via the agent's docker daemon.
                        sh '''
                            $PY -m pytest -q \
                                --junitxml=reports/junit.xml \
                                --cov=dhan_bridge --cov-report=xml:reports/coverage.xml \
                                --cov-report=html:reports/htmlcov --cov-fail-under=80
                        '''
                    }
                    post {
                        always {
                            recordCoverage(tools: [[parser: 'COBERTURA', pattern: 'reports/coverage.xml']],
                                           sourceCodeRetention: 'NEVER')
                        }
                    }
                }

                stage('Safety: no-trade') {
                    steps {
                        mark('Safety: no-trade')
                        // Static grep + runtime SDK guard.
                        sh '$PY -m pytest -q tests/test_no_trade.py tests/test_runtime_guard.py --junitxml=reports/junit-no-trade.xml'
                    }
                }

                stage('DB portability') {
                    steps {
                        mark('DB portability')
                        // A full db-move between two throwaway PostgreSQL servers (16 -> 17),
                        // fingerprint-verified, plus fallback/revert paths. Hard gate: if the
                        // data can't move hosts, the release doesn't ship.
                        sh '''
                            $PY -m pytest -q tests/test_dbmove.py tests/test_portability.py \
                                --junitxml=reports/junit-db-portability.xml
                        '''
                    }
                }

                stage('Safety: gitleaks') {
                    steps {
                        mark('Safety: gitleaks')
                        sh '''
                            docker run --rm --user "$(id -u):$(id -g)" -v "$WORKSPACE:/repo" "$GITLEAKS_IMG" \
                                git /repo --redact --no-banner \
                                --report-format json --report-path /repo/reports/gitleaks.json
                        '''
                    }
                }

                stage('Safety: pip-audit') {
                    steps {
                        mark('Safety: pip-audit')
                        // pip-audit exits non-zero on ANY vuln; the gate fails only on vulns with a fix.
                        sh '''
                            .venv/bin/pip-audit --requirement requirements.txt --require-hashes --disable-pip \
                                --format json --output reports/pip-audit.json || true
                            $PY ci/pip_audit_gate.py reports/pip-audit.json
                        '''
                    }
                }

                stage('Build image') {
                    when { not { buildingTag() } }
                    steps {
                        mark('Build image')
                        sh '''
                            docker build \
                                --build-arg VERSION="$VERSION" --build-arg GIT_SHA="$GIT_SHA" \
                                -t "$IMAGE:$VERSION" -t "$IMAGE:$GIT_SHA" .
                        '''
                    }
                }

                stage('Promote image') {
                    when { buildingTag() }
                    steps {
                        mark('Promote image')
                        withCredentials([usernamePassword(credentialsId: 'registry-creds',
                                                          usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                            sh '''
                                echo "$REG_PASS" | docker login "${REGISTRY%%/*}" -u "$REG_USER" --password-stdin
                                if ! docker pull "$IMAGE:$GIT_SHA"; then
                                    echo "ERROR: $IMAGE:$GIT_SHA not found: this commit was never built on main."
                                    echo "Release tags never rebuild - tag a commit that main's pipeline built and pushed."
                                    exit 1
                                fi
                                docker tag "$IMAGE:$GIT_SHA" "$IMAGE:$VERSION"
                            '''
                        }
                    }
                    post {
                        always { sh 'docker logout "${REGISTRY%%/*}" || true' }
                    }
                }

                stage('Image scan') {
                    steps {
                        mark('Image scan')
                        sh '''
                            status=0
                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                -v trivy-cache:/root/.cache/trivy \
                                -v "$WORKSPACE/reports:/reports" \
                                "$TRIVY_IMG" image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
                                --exit-code 1 --format json --output /reports/trivy.json "$IMAGE:$GIT_SHA" || status=$?
                            docker run --rm -v "$WORKSPACE/reports:/reports" "$TRIVY_IMG" \
                                convert --format table --output /reports/trivy.txt /reports/trivy.json || true
                            # trivy runs as root (docker.sock); hand the reports back to the agent user
                            docker run --rm --entrypoint chown -v "$WORKSPACE/reports:/reports" "$TRIVY_IMG" \
                                -R "$(id -u):$(id -g)" /reports || true
                            cat reports/trivy.txt || true
                            exit $status
                        '''
                    }
                }

                stage('Smoke test') {
                    steps {
                        mark('Smoke test')
                        // On tags this runs against the promoted (pulled) image. The image talks
                        // to a throwaway postgres on a private docker network, exactly like prod
                        // talks to Supabase: DATABASE_URL from /run/secrets/db.env.
                        sh '''
                            name="dhan-smoke-$BUILD_NUMBER-$GIT_SHA"
                            net="$name-net"; pg="$name-pg"
                            pgpass="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
                            cleanup() {
                                docker logs "$name" > reports/smoke.log 2>&1 || true
                                docker rm -f "$name" "$pg" >/dev/null 2>&1 || true
                                docker network rm "$net" >/dev/null 2>&1 || true
                            }
                            trap cleanup EXIT
                            docker network create "$net" >/dev/null
                            POSTGRES_PASSWORD="$pgpass" docker run -d --name "$pg" --network "$net" \\
                                -e POSTGRES_PASSWORD "$(echo "$PG_TEST_IMAGE" | sed 's/{major}/16/')" >/dev/null
                            mkdir -p .smoke/secrets
                            printf 'DHAN_CLIENT_ID=0000000000\\nDHAN_ACCESS_TOKEN=smoke-test-not-a-real-token\\n' > .smoke/secrets/dhan.env
                            printf 'DATABASE_URL=postgresql://postgres:%s@%s:5432/postgres?sslmode=disable\\n' "$pgpass" "$pg" > .smoke/secrets/db.env
                            for i in $(seq 1 30); do docker exec "$pg" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
                            docker create --name "$name" --network "$net" -e BRIDGE_MODE=replay "$IMAGE:$GIT_SHA" >/dev/null
                            # docker cp (not a bind mount) works even when the agent itself runs in a container.
                            docker cp .smoke/secrets "$name:/run/secrets"
                            docker start "$name" >/dev/null
                            for i in $(seq 1 30); do
                                # docker exec => loopback peer => full /health payload; --strict needs
                                # db_ok (SELECT 1 + alembic at head) and token_ok == null in replay
                                if docker exec "$name" python -m dhan_bridge.health probe --strict \\
                                       --expect-mode replay --expect-version "$VERSION"; then
                                    exit 0
                                fi
                                sleep 2
                            done
                            echo "smoke test failed: /health never became healthy"
                            exit 1
                        '''
                    }
                }

                // TODO(replay-smoke): a later release adds replay market data and golden screener
                // output. This stage will replay a recorded session through the built image and
                // diff the screener results against tests/golden/. Keep it right after 'Smoke test'.
                stage('Smoke: replay golden screener (TODO)') {
                    when { expression { return false } }
                    steps {
                        echo 'TODO: replay recorded ticks and compare screener output with golden files'
                    }
                }

                stage('Push') {
                    when {
                        anyOf {
                            branch 'main'
                            tag pattern: 'v*', comparator: 'GLOB'
                        }
                    }
                    steps {
                        mark('Push')
                        withCredentials([usernamePassword(credentialsId: 'registry-creds',
                                                          usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                            sh '''
                                echo "$REG_PASS" | docker login "${REGISTRY%%/*}" -u "$REG_USER" --password-stdin
                                if [ -n "${TAG_NAME:-}" ]; then
                                    docker push "$IMAGE:$VERSION"    # promotion: :$GIT_SHA already exists
                                else
                                    docker push "$IMAGE:$GIT_SHA"
                                fi
                            '''
                        }
                    }
                    post {
                        always { sh 'docker logout "${REGISTRY%%/*}" || true' }
                    }
                }

                stage('Deploy staging') {
                    when { branch 'main' }
                    steps {
                        mark('Deploy staging')
                        script { env.DEPLOY_TARGET = 'staging' }
                        sshagent(credentials: ['staging-ssh']) {
                            sh '''
                                scp $SSH_OPTS deploy/docker-compose.staging.yml "$STAGING_HOST:$REMOTE_DIR/docker-compose.yml"
                                scp $SSH_OPTS deploy/nginx.conf "$STAGING_HOST:$REMOTE_DIR/nginx.conf"
                                scp $SSH_OPTS deploy/remote-deploy.sh deploy/ops.sh deploy/db-move.sh deploy/backup-nightly.sh "$STAGING_HOST:$REMOTE_DIR/"
                                ssh $SSH_OPTS "$STAGING_HOST" "bash -s -- deploy $GIT_SHA" < deploy/remote-deploy.sh
                                ssh $SSH_OPTS "$STAGING_HOST" "bash -s -- health 90 --expect-version $VERSION" < deploy/remote-deploy.sh
                            '''
                        }
                    }
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'reports/junit*.xml'
                    archiveArtifacts allowEmptyArchive: true,
                                     artifacts: 'reports/junit*.xml, reports/coverage.xml, reports/htmlcov/**, reports/trivy.*, reports/pip-audit.json, reports/gitleaks.json, reports/smoke.log'
                    sh 'rm -rf .smoke'
                }
            }
        }

        // Waits for a human with NO executor held.
        stage('Approve prod') {
            agent none
            when {
                beforeAgent true
                tag pattern: 'v\\d+\\.\\d+\\.\\d+', comparator: 'REGEXP'
            }
            steps {
                script {
                    mark('Approve prod')
                    timeout(time: 4, unit: 'HOURS') {
                        env.APPROVER = input(
                            message: "Deploy dhan_bridge ${env.VERSION} (${env.GIT_SHA}) to PRODUCTION?",
                            ok: 'Deploy to prod',
                            submitter: 'admins',
                            submitterParameter: 'APPROVER')
                    }
                }
            }
        }

        stage('Deploy prod') {
            agent any
            when {
                beforeAgent true
                tag pattern: 'v\\d+\\.\\d+\\.\\d+', comparator: 'REGEXP'
            }
            steps {
                checkout scm    // possibly a different agent/workspace than the CI stages
                script {
                    mark('Deploy prod')
                    env.DEPLOY_TARGET = 'prod'

                    // Market-hours guard, evaluated AFTER approval, inside the promoted image.
                    withCredentials([usernamePassword(credentialsId: 'registry-creds',
                                                      usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                        sh '''
                            echo "$REG_PASS" | docker login "${REGISTRY%%/*}" -u "$REG_USER" --password-stdin
                            docker pull "$IMAGE:$VERSION"
                            docker logout "${REGISTRY%%/*}" || true
                        '''
                    }
                    def guard = sh(returnStatus: true,
                                   script: 'docker run --rm "$IMAGE:$VERSION" python -m dhan_bridge.calendar deploy-guard')
                    if (guard == 3) {
                        def reason = (params.OVERRIDE_REASON ?: '').trim()
                        if (params.FORCE_MARKET_HOURS_DEPLOY && reason) {
                            def note = "MARKET-HOURS OVERRIDE (approved by ${env.APPROVER}): ${reason}"
                            currentBuild.description = ((currentBuild.description ?: '') + ' ' + note).trim()
                            echo "WARNING: ${note}"
                        } else {
                            error('Prod deploy blocked: 09:00-15:45 IST on a trading day. ' +
                                  'Re-run with FORCE_MARKET_HOURS_DEPLOY=true and a non-empty OVERRIDE_REASON to override.')
                        }
                    } else if (guard != 0) {
                        error("deploy-guard failed with exit code ${guard}")
                    }

                    sshagent(credentials: ['prod-ssh']) {
                        // Informational; the authoritative record is data/backups/last-deploy.json on the host.
                        env.PREVIOUS = sh(returnStdout: true,
                            script: 'ssh $SSH_OPTS "$PROD_HOST" "bash -s -- current-tag" < deploy/remote-deploy.sh').trim()
                        echo "PREVIOUS=${env.PREVIOUS ?: '(none)'}"

                        // Deploy (takes a consistent pg_dump of schema bridge first) + strict health gate:
                        // HTTP 200, db_ok, and token_ok == true in live mode, within 120 s.
                        def ok = sh(returnStatus: true, script: '''
                            scp $SSH_OPTS deploy/docker-compose.prod.yml "$PROD_HOST:$REMOTE_DIR/docker-compose.yml" &&
                            scp $SSH_OPTS deploy/nginx.conf "$PROD_HOST:$REMOTE_DIR/nginx.conf" &&
                            scp $SSH_OPTS deploy/remote-deploy.sh deploy/ops.sh deploy/db-move.sh deploy/backup-nightly.sh "$PROD_HOST:$REMOTE_DIR/" &&
                            ssh $SSH_OPTS "$PROD_HOST" "bash -s -- deploy $VERSION" < deploy/remote-deploy.sh &&
                            ssh $SSH_OPTS "$PROD_HOST" "bash -s -- health 120 --strict --expect-version $VERSION" < deploy/remote-deploy.sh
                        ''') == 0

                        // Automatic rollback: restores the pre-deploy backup if the schema moved.
                        if (!ok) {
                            def rolledBack = sh(returnStatus: true, script: '''
                                ssh $SSH_OPTS "$PROD_HOST" "bash -s -- rollback" < deploy/remote-deploy.sh &&
                                ssh $SSH_OPTS "$PROD_HOST" "bash -s -- health 120 --strict" < deploy/remote-deploy.sh
                            ''') == 0
                            env.ROLLBACK = rolledBack ?
                                "rolled back to ${env.PREVIOUS ?: 'the recorded previous tag'} (healthy)" :
                                'rollback FAILED or unhealthy - manual action required (docs/RUNBOOK.md)'
                            error("Prod deploy of ${env.VERSION} failed the health gate; ${env.ROLLBACK}")
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            // Top-level agent none: borrow an executor briefly for the notification.
            script {
                node {
                    notifyDiscord()
                    sh 'rm -f .discord-payload.json'
                }
            }
        }
    }
}
