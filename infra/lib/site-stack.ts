import * as cdk from 'aws-cdk-lib/core';
import * as path from 'path';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import { Construct } from 'constructs';

/**
 * U29 — static site stack for Command HQ (the React + Vite + Tailwind SPA).
 *
 * Hosts the built SPA on a *private* S3 bucket fronted by CloudFront. The bucket
 * is never public: CloudFront reaches it through an Origin Access Control (OAC),
 * and a bucket policy grants only that distribution read access. Because the SPA
 * uses client-side routing, CloudFront maps S3's 403/404 (any path that is not a
 * real object) back to `/index.html` with a 200 so deep links resolve in React
 * Router. The Vite build output (packages/web/dist) is uploaded via a
 * BucketDeployment that also invalidates the CloudFront cache on every deploy.
 *
 * Synth-only: this stack is authored and `cdk synth`-verified here; deployment
 * happens through the guarded CI workflow / documented manual commands, never
 * from this environment.
 */
export class SiteStack extends cdk.Stack {
  readonly bucket: s3.Bucket;
  readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ---- Private origin bucket ------------------------------------------------
    // No public access of any kind; CloudFront is the only reader (via OAC).
    this.bucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      publicReadAccess: false,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ---- CloudFront distribution with Origin Access Control -------------------
    // S3BucketOrigin.withOriginAccessControl provisions an OAC and wires the
    // bucket policy so only this distribution can GetObject — no OAI, no public
    // bucket. SPA error routing rewrites 403/404 to /index.html (HTTP 200).
    this.distribution = new cloudfront.Distribution(this, 'SiteDistribution', {
      comment: 'Command HQ SPA (U29)',
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        compress: true,
      },
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(0),
        },
      ],
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    // ---- Deploy the Vite build + invalidate -----------------------------------
    // Sources the SPA build output (packages/web/dist). The deployment uploads to
    // the private bucket and issues a CloudFront invalidation so viewers get the
    // new assets immediately. `npm run build -w @harness/web` must have produced
    // dist/ before `cdk deploy` (the CI workflow guarantees the order).
    new s3deploy.BucketDeployment(this, 'DeployWebApp', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', '..', 'packages', 'web', 'dist'))],
      destinationBucket: this.bucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      prune: true,
    });

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'SiteBucketName', { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
    });
    new cdk.CfnOutput(this, 'SiteUrl', {
      value: `https://${this.distribution.distributionDomainName}`,
    });
  }
}
