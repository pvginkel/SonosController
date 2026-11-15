// Tests SonosController's backend in a Kubernetes Job, then builds the sonos-controller image and
// pins it into SonosControllerDeploy, which Argo CD syncs to prd.
//
// The image is built from the tree the suite passed on, so `latest` is tagged at build time and
// there is no promote stage.
//
// Controller config:
//   - Job: SonosController
//   - SCM: pvginkel/SonosController, branch main
//   - Script Path: Jenkinsfile

library identifier: 'JenkinsPipelineUtils', changelog: false

pipeline {
    agent {
        kubernetes {
            inheritFrom 'jenkins-agent kaniko'
            yamlMergeStrategy merge()
            yaml podYaml(templates: ['k8s'])
        }
    }

    options {
        disableConcurrentBuilds(abortPrevious: true)
        skipDefaultCheckout()
        timeout(time: 60, unit: 'MINUTES')
        timestamps()
    }

    triggers {
        githubPush()
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Test') {
            steps {
                script {
                    // The services are containers in the Job's pod and start with the suite, so the
                    // suite runner waits for them (backend/scripts/wait-for-services.py).
                    // PostgreSQL is a sidecar so that pytest has a server to create and drop its
                    // databases on, at the default TEST_DATABASE_ADMIN_URL. Its major version is
                    // production's.
                    modernApp.test(
                        job: 'sonos-controller-validation',
                        install: 'uv sync --locked --no-dev',
                        run: 'uv run --no-sync',
                        suites: ['backend'],
                        services: [
                            [name: 'postgres', image: 'postgres:18-trixie', env: [
                                POSTGRES_PASSWORD: 'postgres',
                            ]],
                        ],
                        env: [
                            // CI is the tst tier; under testing STAGE would default to local-tst.
                            STAGE: 'tst',
                        ],
                        secrets: [],
                    )
                }
            }
        }

        stage('Build sonos-controller image') {
            steps {
                container('kaniko') {
                    script {
                        helmCharts.kaniko2(
                            dockerfile: 'backend/Dockerfile',
                            context: 'backend',
                            destinations: [
                                "registry:5000/sonos-controller:${currentBuild.number}",
                                'registry:5000/sonos-controller:latest',
                            ]
                        )
                    }
                }
            }
        }

        stage('Write image pins') {
            steps {
                container('k8s') {
                    script {
                        cicd.writeVersionPins(repo: 'pvginkel/SonosControllerDeploy', pins: [
                            'config/prd/values.yaml': [
                                'images.sonosController': ":${currentBuild.number}",
                            ],
                        ])
                    }
                }
            }
        }
    }
}
