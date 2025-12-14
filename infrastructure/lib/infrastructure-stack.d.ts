import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
export interface OrganizeDCTechStackProps extends cdk.StackProps {
    domainName?: string;
    certificateArn?: string;
    hostedZoneId?: string;
    cognitoDomainPrefix?: string;
}
export declare class InfrastructureStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props?: OrganizeDCTechStackProps);
}
