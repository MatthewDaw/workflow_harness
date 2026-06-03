export namespace daemon {
	
	export class SessInfo {
	    id: string;
	    name: string;
	    focused: boolean;
	    status: string;
	
	    static createFrom(source: any = {}) {
	        return new SessInfo(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.id = source["id"];
	        this.name = source["name"];
	        this.focused = source["focused"];
	        this.status = source["status"];
	    }
	}

}

