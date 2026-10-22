import unittest
from services.analysis_api.deploy import template,release_template


class DeploymentTests(unittest.TestCase):
    def setUp(self):self.stack=template('probe-bucket','ami-test','subnet-test','vpc-test','phase3b-test')

    def test_api_is_authorized_when_first_exposed(self):
        resources=self.stack['Resources']
        route=resources['Route']['Properties']
        self.assertEqual(route['AuthorizationType'],'JWT')
        self.assertEqual(route['AuthorizationScopes'],['aws.cognito.signin.user.admin'])
        self.assertTrue(resources['Pool']['Properties']['AdminCreateUserConfig']['AllowAdminCreateUserOnly'])
        self.assertFalse(resources['Client']['Properties']['GenerateSecret'])
        self.assertNotIn('AccessLogSettings',resources['Stage']['Properties'])

    def test_worker_has_no_inbound_and_two_independent_time_guards(self):
        resources=self.stack['Resources'];worker=resources['Worker']['Properties']
        self.assertEqual(resources['WorkerSecurity']['Properties']['SecurityGroupIngress'],[])
        self.assertEqual(worker['MetadataOptions']['HttpTokens'],'required')
        self.assertEqual(worker['InstanceInitiatedShutdownBehavior'],'stop')
        script=worker['UserData']['Fn::Base64']['Fn::Sub']
        self.assertIn('shutdown -h +60',script)
        self.assertIn("Environment=BUCKET=probe-bucket",script)
        self.assertIn("Environment=TABLE=${Table}",script)
        self.assertIn('systemctl enable --now kiroclops-worker.service',script)
        self.assertIn('ControllerStates',resources)
        self.assertIn('GuardPermission',resources['Worker']['DependsOn'])
        self.assertIn('4500',resources['Guard']['Properties']['Code']['ZipFile'])

    def test_no_backup_or_content_logging_is_created(self):
        resources=self.stack['Resources']
        self.assertNotIn('PointInTimeRecoverySpecification',resources['Table']['Properties'])
        self.assertNotIn('NATGateway',{r['Type'] for r in resources.values()})
        self.assertEqual(resources['Queue']['Properties']['MessageRetentionPeriod'],3600)

    def test_replacement_download_is_exactly_authorized(self):
        for revision in (2,3):
            spec=release_template(self.stack,'probe-bucket','phase3b-test',revision)
            prefix='phase3b-test/release-'+str(revision).zfill(2)
            worker='Worker'+str(revision)
            script=spec['Resources'][worker]['Properties']['UserData']['Fn::Base64']['Fn::Sub']
            self.assertIn("'"+prefix+"/bundle.tar'",script)
            statements=spec['Resources']['WorkerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
            self.assertIn('arn:aws:s3:::probe-bucket/'+prefix+'/bundle.tar',[s['Resource'] for s in statements])
            self.assertEqual(spec['Resources']['Controller']['Properties']['Environment']['Variables']['WORKER_ID'],{'Ref':worker})
            self.assertIn('Environment=DEPLOYMENT_REVISION='+str(revision),script)

    def test_reusable_beta_stops_instead_of_destroys_owned_worker(self):
        resources=template('probe-bucket','ami-test','subnet-test','vpc-test','phase3b-test',invited_beta=True)['Resources']
        code=resources['Guard']['Properties']['Code']['ZipFile']
        self.assertIn('4500',code);self.assertIn('ec2.stop_instances(',code)
        self.assertNotIn('terminate_instances(',code)
        policies=str(resources['GuardRole']['Properties']['Policies'])
        self.assertIn('ec2:StopInstances',policies);self.assertNotIn('ec2:TerminateInstances',policies)


if __name__=='__main__':unittest.main()
