import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { SiteStack } from '../lib/site-stack';

/**
 * U29 assertions: the synthesized site stack must host the SPA on a *private*
 * S3 bucket reached only through CloudFront's Origin Access Control, with SPA
 * deep-link routing mapping S3's 403/404 back to /index.html (HTTP 200).
 */
function synth(): Template {
  const app = new cdk.App();
  const stack = new SiteStack(app, 'TestSiteStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  return Template.fromStack(stack);
}

describe('SiteStack', () => {
  const template = synth();

  test('origin bucket blocks all public access', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('CloudFront reaches the bucket via an Origin Access Control (not OAI)', () => {
    // An OAC resource is created...
    template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
    template.hasResourceProperties('AWS::CloudFront::OriginAccessControl', {
      OriginAccessControlConfig: Match.objectLike({
        OriginAccessControlOriginType: 's3',
        SigningBehavior: 'always',
        SigningProtocol: 'sigv4',
      }),
    });
    // ...and the legacy Origin Access Identity is NOT used.
    template.resourceCountIs('AWS::CloudFront::CloudFrontOriginAccessIdentity', 0);

    // The distribution wires that OAC onto its S3 origin and serves index.html.
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultRootObject: 'index.html',
        Origins: Match.arrayWith([
          Match.objectLike({
            OriginAccessControlId: Match.anyValue(),
            S3OriginConfig: Match.objectLike({ OriginAccessIdentity: '' }),
          }),
        ]),
      }),
    });
  });

  test('the origin bucket policy grants read only to the CloudFront distribution', () => {
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'cloudfront.amazonaws.com' },
            Action: 's3:GetObject',
          }),
        ]),
      }),
    });
  });

  test('SPA error routing maps 403 and 404 to /index.html with HTTP 200', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CustomErrorResponses: Match.arrayWith([
          Match.objectLike({
            ErrorCode: 403,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
          }),
          Match.objectLike({
            ErrorCode: 404,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
          }),
        ]),
      }),
    });
  });
});
